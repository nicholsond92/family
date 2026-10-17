/**
 * Family Hub calendar bridge
 *
 * Publishes Google calendars YOU can see (including ones others shared with
 * you) as iCal links the Family Hub "Shared calendars" card can read.
 * It runs under your own Google account, so no secret address is needed.
 *
 * SETUP (one time, about 5 minutes, on a computer):
 *  1. Go to https://script.google.com while signed in as the Google account
 *     that has the shared calendars. Click "New project".
 *  2. Delete the sample code and paste this whole file in. Click Save.
 *  3. Click Deploy > New deployment. Click the gear next to "Select type"
 *     and choose "Web app". Set:
 *        Execute as:      Me
 *        Who has access:  Anyone
 *     Click Deploy, then Authorize access and allow it. (Google may warn the
 *     app is unverified: click Advanced > Go to project. It's your script.)
 *  4. Copy the "Web app" URL (ends in /exec).
 *  5. In Family Hub > Settings > Shared calendars, add one entry per
 *     calendar, using this as the iCal address:
 *
 *       <web app URL>?key=CHANGE-ME-to-a-long-random-string&id=<calendar ID>
 *
 *     The calendar ID is in Google Calendar > Settings > that calendar >
 *     Integrate calendar > Calendar ID. For example, Custody Calendar:
 *
 *       <web app URL>?key=CHANGE-ME-to-a-long-random-string&id=17d9fa269424187fba0c5a903784afe3437f3a02827137a8343a33e3d9c423ec@group.calendar.google.com
 *
 * Family Hub also sends edits and deletes of these events back here, and
 * this script applies them to the Google calendar as you.
 *
 * UPDATING THIS SCRIPT (keeps the same link): paste the new code, Save, then
 * Deploy > Manage deployments > pencil icon > Version: New version > Deploy.
 *
 * Anyone with the full link (including the key) can read and change that
 * calendar's events, so treat it like a password. To revoke, change KEY below and redeploy
 * (Deploy > Manage deployments > edit > New version), or archive it.
 */

var KEY = 'CHANGE-ME-to-a-long-random-string';
var PAST_DAYS = 30;
var FUTURE_DAYS = 365;

function doGet(e) {
  var p = (e && e.parameter) || {};
  if (p.key !== KEY) {
    return ContentService.createTextOutput('Forbidden');
  }
  var cal = p.id ? CalendarApp.getCalendarById(p.id) : null;
  if (!cal) {
    return ContentService.createTextOutput('Calendar not found');
  }
  var now = new Date();
  var start = new Date(now.getTime() - PAST_DAYS * 86400000);
  var end = new Date(now.getTime() + FUTURE_DAYS * 86400000);
  var events = cal.getEvents(start, end);

  var lines = [
    'BEGIN:VCALENDAR',
    'VERSION:2.0',
    'PRODID:-//Family Hub//Calendar bridge//EN',
    'CALSCALE:GREGORIAN',
    'X-WR-CALNAME:' + esc(cal.getName())
  ];
  events.forEach(function (ev) {
    var s, t;
    lines.push('BEGIN:VEVENT');
    // Recurring instances share an iCal UID; make each occurrence unique.
    lines.push('UID:' + ev.getId() + '-' + ev.getStartTime().getTime());
    lines.push('DTSTAMP:' + utc(now));
    if (ev.isAllDayEvent()) {
      s = ev.getAllDayStartDate();
      t = ev.getAllDayEndDate();
      lines.push('DTSTART;VALUE=DATE:' + day(s));
      lines.push('DTEND;VALUE=DATE:' + day(t));
    } else {
      lines.push('DTSTART:' + utc(ev.getStartTime()));
      lines.push('DTEND:' + utc(ev.getEndTime()));
    }
    lines.push('SUMMARY:' + esc(ev.getTitle() || 'Busy'));
    if (ev.getLocation()) lines.push('LOCATION:' + esc(ev.getLocation()));
    if (ev.getDescription()) lines.push('DESCRIPTION:' + esc(ev.getDescription()));
    lines.push('END:VEVENT');
  });
  lines.push('END:VCALENDAR');

  return ContentService.createTextOutput(lines.join('\r\n'))
      .setMimeType(ContentService.MimeType.ICAL);
}

/**
 * Edits and deletes sent by Family Hub. Body is JSON:
 *   {action: "update"|"delete", uid, title, location, notes,
 *    all_day, date: "YYYY-MM-DD", start, end (ISO with offset)}
 * The uid is the one this script put in the feed (event id + start time).
 */
function doPost(e) {
  var p = (e && e.parameter) || {};
  if (p.key !== KEY) return json({ok: false, error: 'wrong key'});
  var cal = p.id ? CalendarApp.getCalendarById(p.id) : null;
  if (!cal) return json({ok: false, error: 'calendar not found'});
  var body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return json({ok: false, error: 'bad request'});
  }
  var ev = findEvent(cal, String(body.uid || ''));
  if (!ev) {
    return json({ok: false, error: 'that event is no longer on the Google calendar'});
  }
  try {
    if (body.action === 'delete') {
      ev.deleteEvent();
      return json({ok: true});
    }
    if (body.action !== 'update') return json({ok: false, error: 'unknown action'});
    ev.setTitle(body.title || '');
    ev.setLocation(body.location || '');
    ev.setDescription(body.notes || '');
    if (body.all_day) {
      var d = body.date.split('-');
      ev.setAllDayDate(new Date(Number(d[0]), Number(d[1]) - 1, Number(d[2])));
    } else {
      ev.setTime(new Date(body.start), new Date(body.end));
    }
    return json({ok: true, uid: ev.getId() + '-' + ev.getStartTime().getTime()});
  } catch (err) {
    return json({ok: false, error: String(err && err.message || err)});
  }
}

function findEvent(cal, uid) {
  var cut = uid.lastIndexOf('-');
  if (cut < 0) return null;
  var id = uid.slice(0, cut);
  var ms = Number(uid.slice(cut + 1));
  if (!ms) return null;
  var matches = cal.getEvents(new Date(ms), new Date(ms + 60000)).filter(function (ev) {
    return ev.getId() === id && ev.getStartTime().getTime() === ms;
  });
  return matches.length ? matches[0] : null;
}

function json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
      .setMimeType(ContentService.MimeType.JSON);
}

function utc(d) {
  return Utilities.formatDate(d, 'UTC', "yyyyMMdd'T'HHmmss'Z'");
}

function day(d) {
  // All-day dates are midnight in the calendar's own timezone.
  return Utilities.formatDate(d, Session.getScriptTimeZone(), 'yyyyMMdd');
}

function esc(text) {
  return String(text)
      .replace(/\\/g, '\\\\')
      .replace(/;/g, '\\;')
      .replace(/,/g, '\\,')
      .replace(/\r?\n/g, '\\n');
}
