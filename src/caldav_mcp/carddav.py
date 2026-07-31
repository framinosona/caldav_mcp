"""CardDAV (RFC 6352) support: addressbook discovery, contact CRUD via vCard.

Uses a caldav.DAVClient the same way the CalDAV side does (see
mcp_server.py's CalDAVMCPServer._client) - the python `caldav` package
itself has no AddressBook/vCard client at all, only a generic DAVClient
WebDAV transport (confirmed via source inspection: davclient.py's own
comment notes it "can perhaps be used for vCards and other WebDAV purposes"
but implements nothing beyond raw PROPFIND/REPORT/PUT/DELETE). This module
builds the CardDAV layer directly on top of that, using `vobject` (not
`icalendar` - vCard and iCalendar are sibling formats with separate
libraries) for vCard parsing/building.

iCloud specifically requires a SEPARATE DAVClient pointed at its own
CardDAV entrypoint (contacts.icloud.com), not the CalDAV one
(caldav.icloud.com) - confirmed live: PROPFIND for addressbook-home-set
against the principal resolved from the CalDAV entrypoint returns a plain
404, even though calendar-home-set on that same principal resource works
fine. iCloud genuinely segregates the two services onto different
principals. See mcp_server.py's `_carddav_client` / `MCP_CARDDAV_URI` for
how the caller wires this up.

Live-verified against a real iCloud account (48 real contacts; full
create/read/update/delete cycle, exercised twice independently, both
end to end) before being wired into the MCP server.
"""
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import vobject

DAV_NS = "{DAV:}"
CARD_NS = "{urn:ietf:params:xml:ns:carddav}"

_CURRENT_USER_PRINCIPAL_BODY = """<?xml version="1.0" encoding="utf-8" ?>
<D:propfind xmlns:D="DAV:">
  <D:prop>
    <D:current-user-principal/>
  </D:prop>
</D:propfind>"""

_ADDRESSBOOK_HOME_SET_BODY = """<?xml version="1.0" encoding="utf-8" ?>
<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:prop>
    <C:addressbook-home-set/>
  </D:prop>
</D:propfind>"""

_ADDRESSBOOK_LIST_BODY = """<?xml version="1.0" encoding="utf-8" ?>
<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:prop>
    <D:resourcetype/>
    <D:displayname/>
  </D:prop>
</D:propfind>"""

_ADDRESSBOOK_QUERY_ALL_BODY = """<?xml version="1.0" encoding="utf-8" ?>
<C:addressbook-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:prop>
    <D:getetag/>
    <C:address-data/>
  </D:prop>
  <C:filter test="anyof">
  </C:filter>
</C:addressbook-query>"""


def discover_addressbooks(client, base_url: str) -> Dict[str, Dict[str, str]]:
    """Discover all addressbook collections for the authenticated user.

    Returns {addressbook_id: {"url": ..., "name": ...}}, where addressbook_id
    is the last non-empty path segment of the collection URL (there is no
    separate opaque ID in CardDAV, matching the same "no separate ID"
    situation as CalDAV calendars in this same codebase).
    """
    principal_resp = client.propfind(base_url, props=_CURRENT_USER_PRINCIPAL_BODY, depth=0)
    principal_href = _find_href(principal_resp, f"{DAV_NS}current-user-principal")
    if not principal_href:
        return {}
    principal_url = urljoin(base_url, principal_href)

    home_resp = client.propfind(principal_url, props=_ADDRESSBOOK_HOME_SET_BODY, depth=0)
    home_href = _find_href(home_resp, f"{CARD_NS}addressbook-home-set")
    if not home_href:
        return {}
    home_url = urljoin(base_url, home_href)

    list_resp = client.propfind(home_url, props=_ADDRESSBOOK_LIST_BODY, depth=1)
    objs = list_resp.find_objects_and_props()

    result: Dict[str, Dict[str, str]] = {}
    for href, props in objs.items():
        is_addressbook = False
        displayname = None
        for tag, el in props.items():
            if tag == f"{DAV_NS}resourcetype":
                for child in el:
                    if child.tag == f"{CARD_NS}addressbook":
                        is_addressbook = True
            elif tag == f"{DAV_NS}displayname":
                displayname = el.text
        if not is_addressbook:
            continue
        # Resolve relative to home_url (the per-partition host PROPFIND was
        # actually sent to, e.g. p171-contacts.icloud.com), NOT the original
        # base_url - iCloud's generic front door (contacts.icloud.com)
        # transparently proxies GET/PROPFIND but rejects PUT/DELETE with 403
        # when hit directly, so resolving against the wrong host only shows
        # up as a write-path failure, not a discovery-time error.
        url = urljoin(home_url, href)
        addressbook_id = [p for p in url.rstrip("/").split("/") if p][-1]
        result[addressbook_id] = {"url": url, "name": displayname or addressbook_id}

    return result


def _find_href(propfind_resp, target_tag: str) -> Optional[str]:
    objs = propfind_resp.find_objects_and_props()
    for _href, props in objs.items():
        for tag, el in props.items():
            if tag == target_tag:
                href_el = el.find(f"{DAV_NS}href")
                if href_el is not None and href_el.text:
                    return href_el.text
    return None


def list_vcards(client, addressbook_url: str) -> List[Dict[str, str]]:
    """Fetch every vCard in an addressbook in one REPORT round-trip.

    Returns [{"href": ..., "etag": ..., "vcard_text": ...}, ...].
    """
    resp = client.report(addressbook_url, query=_ADDRESSBOOK_QUERY_ALL_BODY, depth=1)
    objs = resp.find_objects_and_props()
    result = []
    for href, props in objs.items():
        vcard_text = None
        etag = None
        for tag, el in props.items():
            if tag == f"{CARD_NS}address-data":
                vcard_text = el.text
            elif tag == f"{DAV_NS}getetag":
                etag = el.text
        if vcard_text:
            result.append({"href": href, "etag": etag, "vcard_text": vcard_text})
    return result


def contact_resource_url(addressbook_url: str, contact_id: str) -> str:
    return addressbook_url.rstrip("/") + "/" + contact_id + ".vcf"


def serialize_contact(vcard_text: str, addressbook_id: str, resource_url: Optional[str] = None,
                       etag: Optional[str] = None) -> Dict[str, Any]:
    """Parse a raw vCard into the CanonicalContact-shaped dict.

    Wrapped by callers in try/except - a single malformed vCard (seen in
    real-world exports, e.g. from BusyContacts) should not fail the whole
    list_contacts call.
    """
    v = vobject.readOne(vcard_text)

    fn = getattr(v, "fn", None)
    display_name = fn.value if fn is not None else None

    given_name = family_name = None
    if hasattr(v, "n"):
        name = v.n.value
        given_name = name.given or None
        family_name = name.family or None

    if not display_name:
        display_name = " ".join(p for p in [given_name, family_name] if p) or None

    emails = []
    for e in v.contents.get("email", []):
        types = e.params.get("TYPE", [])
        label = _label_from_types(types)
        emails.append({"email": e.value, "label": label})

    phones = []
    for t in v.contents.get("tel", []):
        types = t.params.get("TYPE", [])
        label = _label_from_types(types)
        phones.append({"number": t.value, "label": label})

    org = None
    if hasattr(v, "org"):
        org_val = v.org.value
        org = org_val[0] if isinstance(org_val, list) and org_val else (org_val or None)

    job_title = getattr(v, "title", None)
    job_title = job_title.value if job_title is not None else None

    notes = getattr(v, "note", None)
    notes = notes.value if notes is not None else None

    uid = getattr(v, "uid", None)
    uid = uid.value if uid is not None else None

    return {
        "id": uid,
        "addressbook_id": addressbook_id,
        "display_name": display_name,
        "given_name": given_name,
        "family_name": family_name,
        "emails": emails,
        "phones": phones,
        "organization": org or None,
        "job_title": job_title,
        "notes": notes,
        "uid": uid,
        "etag": etag,
        "resource_url": resource_url,
    }


_LABEL_ALIASES = {"HOME": "home", "WORK": "work", "CELL": "mobile", "MOBILE": "mobile"}


def _label_from_types(types) -> Optional[str]:
    if isinstance(types, str):
        types = [types]
    for t in types or []:
        mapped = _LABEL_ALIASES.get(t.upper())
        if mapped:
            return mapped
    return "other" if types else None


_LABEL_TO_VCARD_TYPE = {"home": "HOME", "work": "WORK", "mobile": "CELL", "other": "OTHER"}


def build_vcard(contact_data: Dict[str, Any]) -> str:
    """Build a vCard 3.0 text body from a CanonicalContact-shaped dict.

    `id` is REQUIRED (becomes UID) - the caller must generate one on
    create, same asymmetry already documented for CalDAV events in this
    codebase (Outlook/Google assign IDs server-side, Apple does not).
    """
    card = vobject.vCard()

    display_name = contact_data.get("display_name")
    given_name = contact_data.get("given_name")
    family_name = contact_data.get("family_name")
    if not display_name:
        display_name = " ".join(p for p in [given_name, family_name] if p) or "(no name)"
    card.add("fn").value = display_name

    if given_name or family_name:
        n = card.add("n")
        n.value = vobject.vcard.Name(family=family_name or "", given=given_name or "")

    card.add("uid").value = contact_data["id"]

    for e in contact_data.get("emails") or []:
        field = card.add("email")
        field.value = e["email"]
        label = e.get("label")
        if label:
            field.type_param = _LABEL_TO_VCARD_TYPE.get(label, label.upper())

    for p in contact_data.get("phones") or []:
        field = card.add("tel")
        field.value = p["number"]
        label = p.get("label")
        if label:
            field.type_param = _LABEL_TO_VCARD_TYPE.get(label, label.upper())

    if contact_data.get("organization"):
        card.add("org").value = [contact_data["organization"]]
    if contact_data.get("job_title"):
        card.add("title").value = contact_data["job_title"]
    if contact_data.get("notes"):
        card.add("note").value = contact_data["notes"]

    return card.serialize()
