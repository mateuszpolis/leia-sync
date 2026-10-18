# How LeIA works (for whoever maintains the sync)

Distilled from a read-only scan of the site on 2026-10-10. The script hard-codes no course
ids or names.

## Basics

- `https://leia.pw.edu.pl` is Moodle 4.5, theme `okno`, UI in Polish.
- Login goes through USOS CAS: `GET /login/index.php?authokno=okno` redirects to
  `cas.usos.pw.edu.pl/cas/login?service=...`; posting the form there (username, password and
  its hidden fields) redirects back with a ticket and sets the Moodle session cookie.
- Every page carries `M.cfg = {... "sesskey":"...", "userId":N ...}` in an inline script.
  No session: pages redirect to `/login/index.php`; AJAX returns `servicerequireslogin`.
- Token and mobile web services are disabled. The only API is the browser's own endpoint:
  `POST /lib/ajax/service.php?sesskey=<sesskey>&info=<method>` with body
  `[{"index":0,"methodname":"<method>","args":{...}}]`.

## AJAX methods that work

| Method | Used for |
|---|---|
| `core_course_get_enrolled_courses_by_timeline_classification` `{offset:0, limit:0, classification:"all", sort:"fullname"}` | course list (the `/my/courses.php` HTML is only a placeholder) |
| `core_courseformat_get_state` `{courseid}` | JSON string with `section[]` (`number`, `title`, `cmlist`) and `cm[]` (`id`, `name`, `module`, `url`, `uservisible`) |
| `core_calendar_get_calendar_monthly_view` `{year, month}` | every calendar event of a month, any month |
| `message_popup_get_popup_notifications` `{useridto:0, limit}` | notifications; reading them this way does not mark them read |
| `core_calendar_get_action_events_by_timesort`, `core_course_get_updates_since`, `mod_forum_get_discussion_posts` | work, not used by the script |

Not available (`servicenotavailable`): `core_course_get_contents`, `mod_assign_*`, `mod_quiz_*`,
grade reports, completion status, forum discussion lists. Those come from HTML.

In `cm[]`, `module` is the machine type (`assign`, `quiz`, ...); `modname` is the localized label.
`url` is missing for modules without their own page.

## Pages

| What | Path |
|---|---|
| Dashboard | `/my/` |
| Course | `/course/view.php?id=<courseid>` |
| Activity | `/mod/<type>/view.php?id=<cmid>` |
| Forum thread | `/mod/forum/discuss.php?d=<id>` |
| Whole book on one page | `/mod/book/tool/print/index.php?id=<cmid>` |
| Finished quiz attempt | `/mod/quiz/review.php?attempt=<id>` |
| Course grades | `/grade/report/user/index.php?id=<courseid>` (`table.user-grade`) |
| Files | `/pluginfile.php/<ctx>/<component>/<area>/<itemid>/<path>` (itemid: revision for `content` areas, chapter/post id elsewhere) |
| Site announcements | `/?redirect=0`, block `block_infoannounce` |

Main content of any page is `#region-main`.

## Module types seen

- `assign`: description, attachments, and a status table (rows "Status przesłanego zadania",
  "Stan oceniania", "Pozostały czas", ...). The description is visible before the task opens.
- `quiz`: the view page has description, open/close dates, grading method, pass mark and past
  attempts. Questions are only visible inside an attempt, so they cannot be mirrored.
- `forum`: thread list is a table; each thread is a separate page. Opening a thread marks its
  posts as read.
- `resource`: `view.php` redirects to the file, or returns a page with an embedded player.
- `folder`: nested `ul/li` with `span.fp-filename a` in raw HTML (the browser rewrites it with JS).
- `url`: redirects to an external address, often a live-meeting link. Record, never follow.
- `book`: use the print view. Its `.book_info` block contains the student's name.
- `page`: sometimes a placeholder; the real text can be the intro shown on the course page.
- `label`, `courseheader` (USOS course card): inline on the course page only, in
  `#module-<cmid> .activity-altcontent`.
- `cregs`: course regulations from USOS, a plain page.
- `bigbluebuttonbn`: info page only. Recordings are loaded by JS and are not in the HTML.
- `choice`: question and options; options are hidden until it opens.
- `scheduler`: every row has a booking button; the script does not open it.
- `subsection`: a section nested inside another. Use `cmlist` for ownership, not HTML nesting.

## Never request

`startattempt.php`, `attempt.php`, `processattempt.php`, `action=editsubmission`,
`bbb_view.php` (joins a meeting), `/enrol/`, `logout.php`, `post.php`, anything with `sesskey=` in
the URL (subscribe, star, logout links), `lang=`, the `action.url` field of calendar events.
The only POSTs are the CAS login and the four AJAX methods in `AJAX_READ_ONLY`.
`leia_sync.py --selftest` checks this list.

## Side effects that cannot be avoided

- Activities with a "view" completion condition ("Przejrzyj") flip to done when their page is fetched.
- Forum posts become read when a thread is fetched.
- Moodle logs each page view under the student's account.

## Noise that is filtered

Moodle randomises `mailto:` percent-encoding and assignment `useridlistid` tokens on each
load, and "Pozostały czas" counts down. The script normalises or ignores these so the changelog
only reports real changes. If a run reports changes when nothing changed, diff two runs and
extend `VOLATILE` or the link clean-up in `to_markdown`.
