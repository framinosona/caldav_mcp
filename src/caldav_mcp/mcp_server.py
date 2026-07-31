"""MCP server for CalDAV integration."""
import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from fastmcp import FastMCP
import caldav
import icalendar

from caldav_mcp import carddav

# Canonical attendee role <-> iCalendar ROLE parameter, matching the
# framinosona/calendar-canonical-schema draft's Attendee.role field.
_ROLE_TO_ICAL = {
    "required": "REQ-PARTICIPANT",
    "optional": "OPT-PARTICIPANT",
    "resource": "NON-PARTICIPANT",
}
_ROLE_FROM_ICAL = {v: k for k, v in _ROLE_TO_ICAL.items()}


def _parse_event_datetime(value: str):
    """Parse a start/end string into a date (all-day) or datetime (timed) object.

    caldav.Event/icalendar need an actual date/datetime object, not a raw
    string - and which type you pass is what determines VALUE=DATE
    (all-day) vs VALUE=DATE-TIME (timed) in the resulting iCalendar output.
    A bare "YYYY-MM-DD" (no "T") is treated as all-day.
    """
    if len(value) == 10 and value.count("-") == 2 and "T" not in value:
        return datetime.strptime(value, "%Y-%m-%d").date()
    return datetime.fromisoformat(value)


def _build_vevent_ical(event_data: Dict[str, Any]) -> str:
    """Build a raw VEVENT iCalendar block from event_data.

    Calendar.save_event(**kwargs)'s generic path (component.add(prop, value)
    once per kwarg) can't represent attendees (needs one ATTENDEE line per
    person, each with CN/ROLE/RSVP parameters) or recurrence (needs a real
    icalendar.vRecur object, not a raw string) correctly - so this builds
    the VEVENT directly with the icalendar library instead, verified against
    the actual library behavior (multi-ATTENDEE lines and RRULE round-trip
    correctly this way) before wiring it in here.
    """
    vevent = icalendar.Event()
    vevent.add("uid", event_data["uid"])
    vevent.add("dtstamp", datetime.now(timezone.utc))
    vevent.add("dtstart", _parse_event_datetime(event_data["start"]))
    vevent.add("dtend", _parse_event_datetime(event_data["end"]))
    vevent.add("summary", event_data["summary"])

    if event_data.get("location"):
        vevent.add("location", event_data["location"])
    if event_data.get("description"):
        vevent.add("description", event_data["description"])
    if event_data.get("recurrence"):
        vevent.add("rrule", icalendar.vRecur.from_ical(event_data["recurrence"]))

    for attendee in event_data.get("attendees", []):
        addr = icalendar.vCalAddress(f"mailto:{attendee['email']}")
        if attendee.get("name"):
            addr.params["CN"] = attendee["name"]
        addr.params["ROLE"] = _ROLE_TO_ICAL.get(attendee.get("role", "required"), "REQ-PARTICIPANT")
        addr.params["RSVP"] = "TRUE"
        vevent.add("attendee", addr, encode=0)

    return vevent.to_ical().decode()


def get_event_data(event):
    """Extract basic event data from a CalDAV event.

    This is a helper for methods that need to work with event data directly.
    Most methods should just use the event objects directly.
    """
    return {
        'uid': event.icalendar_component.get('UID') if event.icalendar_component else None,
        'url': getattr(event, 'url', None)
    }


def _serialize_event(event) -> Dict[str, Any]:
    """Convert a caldav Event object into a plain JSON-serializable dict.

    caldav.Event objects (and their underlying icalendar components) are not
    JSON-serializable on their own, so every tool that returns event data
    must go through this rather than returning the raw object.
    """
    comp = getattr(event, "icalendar_component", None)
    if comp is None:
        return {"uid": None, "url": str(getattr(event, "url", "")) or None}

    def _field(name):
        value = comp.get(name)
        return str(value) if value is not None else None

    def _dt(name):
        value = comp.get(name)
        if value is None:
            return None
        dt = getattr(value, "dt", value)
        return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)

    def _is_all_day(name):
        value = comp.get(name)
        if value is None:
            return False
        dt = getattr(value, "dt", value)
        # a date (all-day) has no .hour; a datetime (timed) does
        return not hasattr(dt, "hour")

    def _recurrence():
        rrule = comp.get("RRULE")
        return rrule.to_ical().decode() if rrule is not None else None

    def _attendees():
        raw = comp.get("ATTENDEE")
        if raw is None:
            return []
        entries = raw if isinstance(raw, list) else [raw]
        result = []
        for entry in entries:
            email = str(entry).replace("mailto:", "", 1) if str(entry).lower().startswith("mailto:") else str(entry)
            params = getattr(entry, "params", {}) or {}
            result.append({
                "email": email,
                "name": params.get("CN"),
                "role": _ROLE_FROM_ICAL.get(params.get("ROLE"), "required"),
                "response_status": params.get("PARTSTAT"),
            })
        return result

    return {
        "uid": _field("UID"),
        "summary": _field("SUMMARY"),
        "start": _dt("DTSTART"),
        "end": _dt("DTEND"),
        "all_day": _is_all_day("DTSTART"),
        "description": _field("DESCRIPTION"),
        "location": _field("LOCATION"),
        "recurrence": _recurrence(),
        "attendees": _attendees(),
        "url": str(getattr(event, "url", "")) or None,
    }


class EnvironmentConfigurationProvider:
    def uri(self):
        return os.environ.get("MCP_CALDAV_URI")

    def username(self):
        return os.environ.get("MCP_CALDAV_USERNAME")

    def password(self):
        return os.environ.get("MCP_CALDAV_PASSWORD")

    def carddav_uri(self):
        # iCloud genuinely segregates calendar and contacts onto separate
        # principals per subdomain - confirmed live: PROPFIND for
        # addressbook-home-set against the principal resolved from
        # MCP_CALDAV_URI (caldav.icloud.com) returns 404 Not Found, even
        # though calendar-home-set on that SAME principal works fine. The
        # CardDAV side has to be discovered from its own dedicated
        # entrypoint. Defaults to iCloud's well-known CardDAV entrypoint;
        # override via MCP_CARDDAV_URI for a non-iCloud CardDAV server.
        return os.environ.get("MCP_CARDDAV_URI", "https://contacts.icloud.com/")


class CalDAVMCPServer:
    """MCP server for CalDAV integration."""
    def __init__(self, name="CalDAV MCP Server"):
        """Initialize the server.

        Args:
            name: The name of the server.
        """
        self.mcp = FastMCP(name=name)
        self._client = None
        self._principal = None
        self._carddav_client = None
        self._addressbooks: Dict[str, Dict[str, str]] = {}
        config_provider = EnvironmentConfigurationProvider()

        # Initialize connection
        self.url = config_provider.uri()
        self.username = config_provider.username()
        self.password = config_provider.password()
        self.carddav_url = config_provider.carddav_uri()

        # Connect to server
        self._connect()

        # Register tools - calendar/events
        self.mcp.tool()(self.get_calendars)
        self.mcp.tool()(self.get_events)
        self.mcp.tool()(self.get_event_by_id)
        self.mcp.tool()(self.get_events_in_range)
        self.mcp.tool()(self.search_events)
        self.mcp.tool()(self.create_event)
        self.mcp.tool()(self.delete_event)
        self.mcp.tool()(self.update_event)

        # Register tools - CardDAV/contacts
        self.mcp.tool()(self.get_addressbooks)
        self.mcp.tool()(self.get_contacts)
        self.mcp.tool()(self.get_contact_by_id)
        self.mcp.tool()(self.search_contacts)
        self.mcp.tool()(self.create_contact)
        self.mcp.tool()(self.update_contact)
        self.mcp.tool()(self.delete_contact)

    def _connect(self):
        """Connect to the CalDAV/CardDAV server."""
        self._client = caldav.DAVClient(
            url=self.url,
            username=self.username,
            password=self.password
        )
        self._principal = self._client.principal()

        # Separate DAVClient for CardDAV, pointed at the dedicated CardDAV
        # entrypoint (see EnvironmentConfigurationProvider.carddav_uri) -
        # kept independent from the calendar client rather than reusing it
        # against a different host, since the two services live on
        # different iCloud principals entirely.
        self._carddav_client = caldav.DAVClient(
            url=self.carddav_url,
            username=self.username,
            password=self.password
        )
        try:
            self._addressbooks = carddav.discover_addressbooks(self._carddav_client, self.carddav_url)
        except Exception:
            # Addressbook discovery is best-effort at connect time - a
            # server-side hiccup here shouldn't prevent calendar tools
            # (already working) from being usable. Contact tools will
            # simply report "not found" until the next restart.
            self._addressbooks = {}

    def _get_calendar_by_id(self, calendar_id: str):
        """Get a calendar by its ID."""
        for calendar in self._principal.calendars():
            if getattr(calendar, "id", str(hash(calendar))) == calendar_id:
                return calendar
        return None

    def _get_event_data(self, event):
        """Get basic data from a CalDAV event."""
        return get_event_data(event)

    def _find_event_by_uid(self, calendar, event_id: str):
        """Find an event by UID via a client-side scan.

        calendar.search(uid=..., comp_class=caldav.Event) is the "correct"
        caldav-library way to do this, but iCloud's CalDAV server returns
        '412 Precondition Failed' on that specific REPORT query - a
        server-side quirk, not a library bug. calendar.events() (a plain
        listing, no REPORT filter) works reliably, so filter client-side.
        """
        for event in calendar.events():
            comp = getattr(event, "icalendar_component", None)
            if comp is not None and str(comp.get("UID")) == event_id:
                return event
        return None

    def _get_addressbook_url(self, addressbook_id: str) -> Optional[str]:
        info = self._addressbooks.get(addressbook_id)
        return info["url"] if info else None

    def get_calendars(self) -> Dict[str, Any]:
        """Get all calendars from the CalDAV server.

        Returns:
            A dictionary with a list of calendars.
        """
        if not self._principal:
            return {"error": "Not connected to CalDAV server"}

        try:
            calendars = self._principal.calendars()
            result = []

            for calendar in calendars:
                result.append({
                    "name": getattr(calendar, "name", "Unknown"),
                    "id": getattr(calendar, "id", str(hash(calendar)))
                })

            return {"calendars": result}
        except Exception as e:
            return {"error": str(e)}

    def get_events(self, calendar_id: str) -> Dict[str, Any]:
        """Get all events from a calendar.

        Args:
            calendar_id: The ID of the calendar.

        Returns:
            A dictionary with a list of events.
        """
        if not self._principal:
            return {"error": "Not connected to CalDAV server"}

        try:
            calendar = self._get_calendar_by_id(calendar_id)
            if not calendar:
                return {"error": f"Calendar with ID {calendar_id} not found"}

            # Return the events, serialized to plain dicts
            return {"events": [_serialize_event(e) for e in calendar.events()]}
        except Exception as e:
            return {"error": str(e)}

    def get_event_by_id(self, calendar_id: str, event_id: str) -> Dict[str, Any]:
        """Get a specific event by its ID.

        Args:
            calendar_id: The ID of the calendar containing the event.
            event_id: The ID of the event to retrieve.

        Returns:
            A dictionary with the event details or an error message.
        """
        if not self._principal:
            return {"error": "Not connected to CalDAV server"}

        try:
            calendar = self._get_calendar_by_id(calendar_id)
            if not calendar:
                return {"error": f"Calendar with ID {calendar_id} not found"}

            event = self._find_event_by_uid(calendar, event_id)
            if not event:
                return {"error": f"Event with ID {event_id} not found in calendar {calendar_id}"}

            return {"event": _serialize_event(event)}

        except Exception as e:
            return {"error": f"Error retrieving event: {str(e)}"}

    def get_events_in_range(
        self,
        calendar_id: str,
        start_time: str,
        end_time: str
    ) -> Dict[str, Any]:
        """Get events within a specific time range.

        Args:
            calendar_id: The ID of the calendar to search in.
            start_time: Start time in format YYYYMMDDTHHMMSS+HHMM.
            end_time: End time in format YYYYMMDDTHHMMSS+HHMM.

        Returns:
            A dictionary with a list of events in the specified time range.
        """
        if not self._principal:
            return {"error": "Not connected to CalDAV server"}

        try:
            calendar = self._get_calendar_by_id(calendar_id)
            if not calendar:
                return {"error": f"Calendar with ID {calendar_id} not found"}

            # Parse datetime strings
            start_dt = datetime.strptime(start_time, "%Y%m%dT%H%M%S%z")
            end_dt = datetime.strptime(end_time, "%Y%m%dT%H%M%S%z")

            # Search for events in the time range
            events = calendar.date_search(start=start_dt, end=end_dt)

            return {
                "events": [_serialize_event(e) for e in events],
                "count": len(events),
                "start_time": start_time,
                "end_time": end_time,
                "calendar_id": calendar_id
            }

        except ValueError as ve:
            return {"error": str(ve)}
        except Exception as e:
            return {"error": f"Error getting events in range: {str(e)}"}

    def search_events(self, calendar_id: str, query: str, limit: int = 10) -> Dict[str, Any]:
        """Search for events in a calendar.

        Args:
            calendar_id: The ID of the calendar to search in.
            query: The search query string.
            limit: Maximum number of results to return (default: 10).

        Returns:
            A dictionary with a list of matching events and any errors.
        """
        if not self._principal:
            return {"error": "Not connected to CalDAV server"}

        try:
            calendar = self._get_calendar_by_id(calendar_id)
            if not calendar:
                return {"error": f"Calendar with ID {calendar_id} not found"}

            # Get all events and filter by query
            events = calendar.events()
            scored_events = []

            for event in events:
                if not hasattr(event, 'icalendar_component') or not event.icalendar_component:
                    continue

                ical = event.icalendar_component
                summary = str(ical.get('SUMMARY', '')).lower()
                description = str(ical.get('DESCRIPTION', '')).lower()

                score = 0
                if query.lower() in summary:
                    score += 2  # Higher weight for summary matches
                if query.lower() in description:
                    score += 1

                if score > 0:
                    scored_events.append((score, event))

            # Sort by score (descending) and limit results
            scored_events.sort(key=lambda x: x[0], reverse=True)
            result = [event for _, event in scored_events[:limit]]

            return {
                "events": [_serialize_event(e) for e in result],
                "count": len(result),
                "query": query,
                "calendar_id": calendar_id
            }

        except Exception as e:
            return {"error": f"Error searching events: {str(e)}"}

    def create_event(self, calendar_id: str, event_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new calendar event.

        Args:
            calendar_id: The ID of the calendar to add the event to.
            event_data: Dictionary containing event details. Required: uid,
                summary, start, end. Optional: location, description,
                recurrence (bare RRULE content, e.g.
                "FREQ=WEEKLY;BYDAY=MO;COUNT=10", no "RRULE:" prefix),
                attendees (list of {email, name?, role?} where role is
                "required"|"optional"|"resource", default "required"). For
                an all-day event, pass start/end as bare "YYYY-MM-DD" dates
                (no time component) rather than a full datetime string.

        Returns:
            A dictionary with the created event details or an error message.
        """
        if not self._principal:
            return {"error": "Not connected to CalDAV server"}

        try:
            calendar = self._get_calendar_by_id(calendar_id)
            if not calendar:
                return {"error": f"Calendar with ID {calendar_id} not found"}

            # Validate required fields
            required_fields = ['uid', 'summary', 'start', 'end']
            for field in required_fields:
                if field not in event_data:
                    return {"error": f"Missing required field: {field}"}

            # Built directly with icalendar rather than Calendar.save_event()'s
            # generic **kwargs path - see _build_vevent_ical for why (attendees
            # and recurrence need real icalendar types/multi-value handling
            # that path can't represent).
            calendar.save_event(ical=_build_vevent_ical(event_data))

            # Return the created event data
            return {"event": event_data}

        except ValueError as ve:
            return {"error": str(ve)}
        except Exception as e:
            return {"error": f"Error creating event: {str(e)}"}

    def delete_event(self, calendar_id: str, event_id: str) -> Dict[str, Any]:
        """Delete an event from a calendar.

        Args:
            calendar_id: The ID of the calendar containing the event.
            event_id: The ID of the event to delete.

        Returns:
            A dictionary with a success message or an error message.
        """
        if not self._principal:
            return {"error": "Not connected to CalDAV server"}

        try:
            calendar = self._get_calendar_by_id(calendar_id)
            if not calendar:
                return {"error": f"Calendar with ID {calendar_id} not found"}

            # Find the event by UID (see _find_event_by_uid for why not search())
            event = self._find_event_by_uid(calendar, event_id)
            if not event:
                return {"error": f"Event with ID {event_id} not found in calendar {calendar_id}"}

            # Delete the event
            event.delete()

            return {"message": f"Event {event_id} deleted successfully"}

        except Exception as e:
            return {"error": f"Error deleting event: {str(e)}"}

    def update_event(self, calendar_id: str, event_id: str, event_data: Dict[str, Any]) -> Dict[str, Any]:
        """Replace an existing event with new data while keeping the same event ID.

        Args:
            calendar_id: The ID of the calendar containing the event.
            event_id: The ID of the event to update.
            event_data: Complete event data (same as create_event). Must include all required fields.
                    The event_id in the URL will override any uid in event_data.

        Returns:
            A dictionary with the updated event details or an error message.
        """
        if not self._principal:
            return {"error": "Not connected to CalDAV server"}

        try:
            # Get the calendar
            calendar = self._get_calendar_by_id(calendar_id)
            if not calendar:
                return {"error": f"Calendar with ID {calendar_id} not found"}

            # Find the existing event to delete (see _find_event_by_uid for why not search())
            event = self._find_event_by_uid(calendar, event_id)
            if not event:
                return {"error": f"Event with ID {event_id} not found in calendar {calendar_id}"}

            # Delete the existing event
            event.delete()

            # Create new event with the same ID
            event_data['uid'] = event_id  # Ensure the UID matches the requested event_id
            return self.create_event(calendar_id, event_data)

        except Exception as e:
            return {"error": f"Error updating event: {str(e)}"}

    # ==================== CardDAV / Contacts ====================

    def get_addressbooks(self) -> Dict[str, Any]:
        """Get all addressbooks (CardDAV contact collections) for the account.

        Returns:
            A dictionary with a list of addressbooks.
        """
        if not self._carddav_client:
            return {"error": "Not connected to CardDAV server"}
        try:
            return {
                "addressbooks": [
                    {"id": ab_id, "name": info["name"]}
                    for ab_id, info in self._addressbooks.items()
                ]
            }
        except Exception as e:
            return {"error": str(e)}

    def get_contacts(self, addressbook_id: str, limit: int = 200) -> Dict[str, Any]:
        """Get all contacts from an addressbook.

        Args:
            addressbook_id: The ID of the addressbook (from get_addressbooks).
            limit: Maximum number of results to return (default: 200).

        Returns:
            A dictionary with a list of contacts.
        """
        ab_url = self._get_addressbook_url(addressbook_id)
        if not ab_url:
            return {"error": f"Addressbook with ID {addressbook_id} not found"}
        try:
            raw = carddav.list_vcards(self._carddav_client, ab_url)
            contacts = []
            for item in raw[:limit]:
                try:
                    contacts.append(
                        carddav.serialize_contact(item["vcard_text"], addressbook_id, item["href"], item["etag"])
                    )
                except Exception:
                    # A single malformed real-world vCard (seen in practice
                    # with some BusyContacts exports) shouldn't fail the
                    # whole listing - skip it rather than erroring out.
                    continue
            return {"contacts": contacts}
        except Exception as e:
            return {"error": f"Error listing contacts: {str(e)}"}

    def get_contact_by_id(self, addressbook_id: str, contact_id: str) -> Dict[str, Any]:
        """Get a specific contact by its ID (vCard UID).

        Args:
            addressbook_id: The ID of the addressbook containing the contact.
            contact_id: The UID of the contact to retrieve.

        Returns:
            A dictionary with the contact details or an error message.
        """
        ab_url = self._get_addressbook_url(addressbook_id)
        if not ab_url:
            return {"error": f"Addressbook with ID {addressbook_id} not found"}
        try:
            resource_url = carddav.contact_resource_url(ab_url, contact_id)
            resp = self._carddav_client.request(resource_url, "GET")
            if resp.status == 404:
                return {"error": f"Contact with ID {contact_id} not found in addressbook {addressbook_id}"}
            if resp.status >= 300:
                return {"error": f"Unexpected status {resp.status} fetching contact {contact_id}"}
            contact = carddav.serialize_contact(resp.raw, addressbook_id, resource_url)
            return {"contact": contact}
        except Exception as e:
            return {"error": f"Error retrieving contact: {str(e)}"}

    def search_contacts(self, addressbook_id: str, query: str, limit: int = 10) -> Dict[str, Any]:
        """Search for contacts in an addressbook by name or email substring.

        Args:
            addressbook_id: The ID of the addressbook to search in.
            query: The search query string (matched against display name and emails).
            limit: Maximum number of results to return (default: 10).

        Returns:
            A dictionary with a list of matching contacts.
        """
        ab_url = self._get_addressbook_url(addressbook_id)
        if not ab_url:
            return {"error": f"Addressbook with ID {addressbook_id} not found"}
        try:
            raw = carddav.list_vcards(self._carddav_client, ab_url)
            q = query.lower()
            matches = []
            for item in raw:
                try:
                    contact = carddav.serialize_contact(item["vcard_text"], addressbook_id, item["href"], item["etag"])
                except Exception:
                    continue
                haystack = " ".join([
                    contact.get("display_name") or "",
                    *[e["email"] for e in contact.get("emails") or []],
                ]).lower()
                if q in haystack:
                    matches.append(contact)
                if len(matches) >= limit:
                    break
            return {"contacts": matches, "count": len(matches), "query": query}
        except Exception as e:
            return {"error": f"Error searching contacts: {str(e)}"}

    def create_contact(self, addressbook_id: str, contact_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new contact.

        Args:
            addressbook_id: The ID of the addressbook to add the contact to.
            contact_data: Dictionary containing contact details. Required: id
                (caller-generated UID - CardDAV has no server-assigned ID,
                same asymmetry as create_event). Optional: display_name,
                given_name, family_name, emails ([{email, label?}]), phones
                ([{number, label?}], label one of home|work|mobile|other),
                organization, job_title, notes.

        Returns:
            A dictionary with the created contact details or an error message.
        """
        ab_url = self._get_addressbook_url(addressbook_id)
        if not ab_url:
            return {"error": f"Addressbook with ID {addressbook_id} not found"}
        if not contact_data.get("id"):
            return {"error": "Missing required field: id"}
        try:
            vcard_body = carddav.build_vcard(contact_data)
            resource_url = carddav.contact_resource_url(ab_url, contact_data["id"])
            resp = self._carddav_client.put(resource_url, vcard_body, {"Content-Type": "text/vcard; charset=utf-8"})
            if resp.status >= 300:
                return {"error": f"Unexpected status {resp.status} creating contact"}
            return {"contact": carddav.serialize_contact(vcard_body, addressbook_id, resource_url)}
        except Exception as e:
            return {"error": f"Error creating contact: {str(e)}"}

    def update_contact(self, addressbook_id: str, contact_id: str, contact_data: Dict[str, Any]) -> Dict[str, Any]:
        """Replace an existing contact with new data while keeping the same ID.

        Args:
            addressbook_id: The ID of the addressbook containing the contact.
            contact_id: The UID of the contact to update.
            contact_data: Complete contact data (same shape as create_contact).
                The contact_id parameter overrides any id in contact_data.

        Returns:
            A dictionary with the updated contact details or an error message.
        """
        ab_url = self._get_addressbook_url(addressbook_id)
        if not ab_url:
            return {"error": f"Addressbook with ID {addressbook_id} not found"}
        try:
            contact_data = dict(contact_data)
            contact_data["id"] = contact_id
            vcard_body = carddav.build_vcard(contact_data)
            resource_url = carddav.contact_resource_url(ab_url, contact_id)
            resp = self._carddav_client.put(resource_url, vcard_body, {"Content-Type": "text/vcard; charset=utf-8"})
            if resp.status >= 300:
                return {"error": f"Unexpected status {resp.status} updating contact"}
            return {"contact": carddav.serialize_contact(vcard_body, addressbook_id, resource_url)}
        except Exception as e:
            return {"error": f"Error updating contact: {str(e)}"}

    def delete_contact(self, addressbook_id: str, contact_id: str) -> Dict[str, Any]:
        """Delete a contact from an addressbook.

        Args:
            addressbook_id: The ID of the addressbook containing the contact.
            contact_id: The UID of the contact to delete.

        Returns:
            A dictionary with a success message or an error message.
        """
        ab_url = self._get_addressbook_url(addressbook_id)
        if not ab_url:
            return {"error": f"Addressbook with ID {addressbook_id} not found"}
        try:
            resource_url = carddav.contact_resource_url(ab_url, contact_id)
            resp = self._carddav_client.delete(resource_url)
            if resp.status == 404:
                return {"error": f"Contact with ID {contact_id} not found in addressbook {addressbook_id}"}
            if resp.status >= 300:
                return {"error": f"Unexpected status {resp.status} deleting contact"}
            return {"message": f"Contact {contact_id} deleted successfully"}
        except Exception as e:
            return {"error": f"Error deleting contact: {str(e)}"}

    def run(self, **kwargs):
        """Run the server.

        Args:
            **kwargs: Additional arguments to pass to the FastMCP run method.
        """
        self.mcp.run(**kwargs)
