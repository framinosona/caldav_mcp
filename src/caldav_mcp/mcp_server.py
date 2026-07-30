"""MCP server for CalDAV integration."""
import json
import os
from datetime import datetime
from typing import Dict, List, Optional, Any
from fastmcp import FastMCP
import caldav


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

    return {
        "uid": _field("UID"),
        "summary": _field("SUMMARY"),
        "start": _dt("DTSTART"),
        "end": _dt("DTEND"),
        "description": _field("DESCRIPTION"),
        "location": _field("LOCATION"),
        "url": str(getattr(event, "url", "")) or None,
    }


class EnvironmentConfigurationProvider:
    def uri(self):
        return os.environ.get("MCP_CALDAV_URI")
    
    def username(self):
        return os.environ.get("MCP_CALDAV_USERNAME")
    
    def password(self):
        return os.environ.get("MCP_CALDAV_PASSWORD")
    
        
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
        config_provider = EnvironmentConfigurationProvider()
        
        # Initialize connection
        self.url = config_provider.uri()
        self.username = config_provider.username()
        self.password = config_provider.password()
        
        # Connect to server
        self._connect()
        
        # Register tools
        self.mcp.tool()(self.get_calendars)
        self.mcp.tool()(self.get_events)
        self.mcp.tool()(self.get_event_by_id)
        self.mcp.tool()(self.get_events_in_range)
        self.mcp.tool()(self.search_events)
        self.mcp.tool()(self.create_event)
        self.mcp.tool()(self.delete_event)
        self.mcp.tool()(self.update_event)
        
    def _connect(self):
        """Connect to the CalDAV server."""
        self._client = caldav.DAVClient(
            url=self.url,
            username=self.username,
            password=self.password
        )
        self._principal = self._client.principal()
    
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
            event_data: Dictionary containing event details (uid, summary, start, end, etc.).
            
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

            # NOTE: caldav.Event's own __init__ takes client/url/data/parent/id/props -
            # it does NOT accept uid/dtstart/dtend/summary/etc as kwargs (that only
            # works via Calendar.save_event(), which builds the iCalendar body for you).
            # Also: dtstart/dtend must be actual datetime objects, not raw strings.
            dtstart = datetime.fromisoformat(event_data['start'])
            dtend = datetime.fromisoformat(event_data['end'])

            calendar.save_event(
                uid=event_data['uid'],
                dtstart=dtstart,
                dtend=dtend,
                summary=event_data['summary'],
                location=event_data.get('location', ''),
                description=event_data.get('description', ''),
            )

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
            
    def run(self, **kwargs):
        """Run the server.
        
        Args:
            **kwargs: Additional arguments to pass to the FastMCP run method.
        """
        self.mcp.run(**kwargs)
