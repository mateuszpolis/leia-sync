# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "beautifulsoup4", "markdownify"]
# ///
"""Read-only mirror of a student's LeIA (Moodle) courses into local folders. See README.md."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify

REPO = Path(__file__).resolve().parent

# Anything that starts, submits, joins, enrols or logs out. Checked on every request.
DENY = re.compile(
    r"startattempt|/attempt\.php|processattempt|summary\.php|editsubmission|action=(?!grading\b)\w*(edit|submit|remove|delete|join)"
    r"|bbb_view|/enrol/|logout|sesskey=|/post\.php|subscribe|/mod/lti/|launch\.php|lang=",
    re.I,
)
AJAX_READ_ONLY = {
    "core_course_get_enrolled_courses_by_timeline_classification",
    "core_courseformat_get_state",
    "core_calendar_get_calendar_monthly_view",
    "message_popup_get_popup_notifications",
}
# Sub-pages of a module that are safe to read and worth mirroring.
CHILD = re.compile(r"/mod/forum/discuss\.php\?d=\d+$|/mod/quiz/review\.php\?attempt=\d+(&cmid=\d+)?$")
# Booking-style modules: every row is a POST button, so only the course-page intro is mirrored.
SKIP_MODULES = {"scheduler", "lti"}
# Rows whose text changes every day without the task changing.
VOLATILE = re.compile(r"Pozostały czas|Pozostało|Time remaining|temu\b", re.I)
TERM = re.compile(r"(\d{4})([ZL])\b")


class Denied(Exception):
    pass


def slug(text, limit=60):
    text = text.replace("ł", "l").replace("Ł", "L")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-") or "x"


def term_index(code):
    """2026L (spring 2026) < 2026Z (autumn 2026) < 2027L."""
    m = TERM.search(code or "")
    return int(m[1]) * 2 + (m[2] == "Z") if m else None


def semester_number(shortname, fullname, first_term):
    idx = term_index(shortname) or term_index(fullname)
    # No term in the name (e.g. announcement courses) means first semester.
    return max(1, idx - first_term + 1) if idx else 1


class Client:
    def __init__(self, base, extra_hosts=()):
        self.base = base.rstrip("/")
        self.hosts = {urlparse(self.base).hostname, *extra_hosts}
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "leia-sync (personal read-only mirror)"
        self.sesskey = None

    def _send(self, method, url, _login=False, **kw):
        """One place for every request: host allowlist, deny-list, manual redirects."""
        for _ in range(10):
            host = urlparse(url).hostname
            if host not in self.hosts:
                return None, url  # external target: report it, never fetch it
            if not _login and DENY.search(url):
                raise Denied(url)
            time.sleep(0.3)
            r = self.s.request(method, url, allow_redirects=False, timeout=60, **kw)
            if r.status_code not in (301, 302, 303, 307, 308):
                return r, url
            url, method, kw = urljoin(url, r.headers["Location"]), "GET", {"stream": kw.get("stream", False)}
        raise RuntimeError(f"redirect loop at {url}")

    def get(self, url, **kw):
        r, final = self._send("GET", urljoin(self.base + "/", url), **kw)
        if r is not None and urlparse(final).path == "/login/index.php":
            raise RuntimeError("session expired")
        return r, final

    def login(self, username, password, cas_host):
        self.hosts.add(cas_host)
        r, url = self._send("GET", f"{self.base}/login/index.php?authokno=okno", _login=True)
        form = next((f for f in BeautifulSoup(r.text, "html.parser").find_all("form") if f.find("input", type="password")), None)
        if form is None or urlparse(url).hostname != cas_host:
            raise RuntimeError(f"expected the CAS login form, got {url}")
        data = {i["name"]: i.get("value", "") for i in form.find_all("input") if i.get("name")}
        data[form.find("input", type="password")["name"]] = password
        user_field = form.find("input", attrs={"name": re.compile("user", re.I)}) or form.find("input", type="text")
        data[user_field["name"]] = username
        r, url = self._send("POST", urljoin(url, form.get("action") or url), _login=True, data=data)
        self.hosts.discard(cas_host)
        m = re.search(r'"sesskey":"([^"]+)"', r.text if r is not None else "")
        if not m or not re.search(r'"userId":[1-9]', r.text):
            raise RuntimeError(f"login failed (wrong password, or CAS wants an extra step); ended at {urlparse(url).hostname}{urlparse(url).path}")
        self.sesskey = m[1]

    def ajax(self, method, args):
        if method not in AJAX_READ_ONLY:
            raise Denied(method)
        time.sleep(0.3)
        r = self.s.post(
            f"{self.base}/lib/ajax/service.php",
            params={"sesskey": self.sesskey, "info": method},
            json=[{"index": 0, "methodname": method, "args": args}],
            timeout=60,
        )
        out = r.json()
        out = out[0] if isinstance(out, list) else out
        if out.get("error"):
            raise RuntimeError(f"{method}: {out.get('exception', {}).get('errorcode') or out.get('error')}")
        return out["data"]


class Mirror:
    """Writes files under the data dir and remembers what it wrote, to report changes and prune leftovers."""

    def __init__(self, client, data_dir, state, max_mb):
        self.c, self.root, self.state, self.max_bytes = client, data_dir, state, max_mb * 2**20
        self.written, self.changes = set(), []
        self.cm_paths = {}  # cmid -> markdown path, for timeline links

    def rel(self, path):
        return str(path.relative_to(self.root))

    def write(self, path, text):
        text = re.sub(r"[?&]sesskey=\w+", "", text).rstrip() + "\n"
        self.written.add(path)
        old = path.read_text() if path.exists() else None
        strip = lambda t: "\n".join(l for l in t.splitlines() if not VOLATILE.search(l))
        if old is None:
            self.changes.append(("new", self.rel(path)))
        elif strip(old) != strip(text):
            self.changes.append(("changed", self.rel(path)))
        if old != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def download(self, url, folder):
        """Save a pluginfile into folder, re-downloading only when the server says it changed."""
        url = url.split("#")[0]
        parts = [unquote(p) for p in urlparse(url).path.split("/pluginfile.php/")[-1].split("/")]
        # ponytail: keeps sub-folders after <ctx>/<component>/<area>/<rev>; flat names could collide
        path = folder.joinpath(*[re.sub(r'[\\:*?"<>|]', "_", p) for p in parts[4:] or parts[-1:]])
        if path in self.written:
            return path
        self.written.add(path)
        known = self.state["files"].get(url, {})
        headers = {"If-None-Match": known["etag"]} if path.exists() and known.get("etag") else {}
        r, _ = self.c.get(url, stream=True, headers=headers)
        if r is None or r.status_code == 304:
            return path
        if r.status_code != 200 or int(r.headers.get("Content-Length") or 0) > self.max_bytes:
            self.written.discard(path)
            return None
        body = r.content
        if not path.exists() or hashlib.sha1(path.read_bytes()).digest() != hashlib.sha1(body).digest():
            self.changes.append(("changed" if path.exists() else "new", self.rel(path)))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        self.state["files"][url] = {"etag": r.headers.get("ETag")}
        return path

    def to_markdown(self, node, page_url, md_path, files_dir):
        """HTML fragment -> markdown, with files downloaded and links pointing at the local copies."""
        children = []
        for junk in node.select("script, style, noscript, form input[type=hidden], .activity-navigation, .completion-info, .local-bfaltformat-access-button, nav, .secondary-navigation, .book_info, .comment-area"):
            junk.decompose()
        for tag in node.select("[href], [src]"):
            attr = "href" if tag.has_attr("href") else "src"
            url = urljoin(page_url, tag[attr])
            if url.startswith("mailto:"):  # Moodle re-randomises the percent-encoding on every load
                url = unquote(url)
            tag[attr] = re.sub(r"[?&]useridlistid=\w+", "", url)
            if urlparse(url).hostname not in self.c.hosts or DENY.search(url):
                continue
            if "/pluginfile.php/" in url:
                local = self.download(url, files_dir)
                if local:
                    tag[attr] = os.path.relpath(local, md_path.parent).replace(" ", "%20")
            elif CHILD.search(url.split("#")[0]):
                children.append(url.split("#")[0])
        for video in node.select("video, audio"):  # markdownify drops media tags
            video.replace_with(BeautifulSoup("".join(f'<p><a href="{s["src"]}">media file</a></p>' for s in video.select("source[src]")), "html.parser"))
        md = markdownify(str(node), heading_style="ATX")
        return re.sub(r"\n{3,}", "\n\n", md).strip(), list(dict.fromkeys(children))

    def page(self, url, md_path, title, header="", follow=True):
        """Mirror one page (and its safe sub-pages) to md_path. Returns the markdown body."""
        files_dir = md_path.with_suffix("")
        try:
            r, final = self.c.get(url, stream=True)
        except Denied:
            return ""
        if r is None:
            body = f"External link (not followed): <{final}>"
        elif "html" not in r.headers.get("Content-Type", ""):
            r.close()  # a file behind a redirect, e.g. a resource module
            local = self.download(final, files_dir)
            body = f"File: [{local.name}]({os.path.relpath(local, md_path.parent).replace(' ', '%20')})" if local else f"File too large or unavailable: <{final}>"
        else:
            soup = BeautifulSoup(r.text, "html.parser")
            main = soup.select_one("#region-main") or soup.select_one("[role=main]") or soup.body
            body, children = self.to_markdown(main, final, md_path, files_dir)
            # Sub-pages link back to themselves and to siblings, so only go one level deep.
            for child in children if follow else []:
                name = slug("-".join(v[0] for v in parse_qs(urlparse(child).query).values()))
                kind = Path(urlparse(child).path).stem
                sub = files_dir / f"{kind}-{name}.md"
                self.page(child, sub, f"{title} / {kind} {name}", follow=False)
                body = body.replace(f"]({child})", f"]({os.path.relpath(sub, md_path.parent)})")
        self.write(md_path, f"# {title}\n\n{header}Source: <{url}>\n\n{body}")
        return body

    def prune(self, folder):
        for p in sorted(folder.rglob("*"), reverse=True):
            if p.is_file() and p not in self.written:
                self.changes.append(("removed", self.rel(p)))
                p.unlink()
            elif p.is_dir() and not any(p.iterdir()):
                p.rmdir()


def sync_course(m, course, course_dir):
    c, leia = m.c, course_dir / "leia"
    for d in ("work/solutions", "work/research"):
        (course_dir / d).mkdir(parents=True, exist_ok=True)
    notes = course_dir / "work" / "notes.md"
    if not notes.exists():
        notes.write_text(f"# {course['fullname']} - notes\n")

    state = json.loads(c.ajax("core_courseformat_get_state", {"courseid": course["id"]}))
    cms = {int(cm["id"]): cm for cm in state["cm"]}
    course_url = f"{c.base}/course/view.php?id={course['id']}"
    r, _ = c.get(course_url)
    soup = BeautifulSoup(r.text, "html.parser")

    index, tasks = [], []
    for sec in sorted(state["section"], key=lambda s: s["number"]):
        sec_dir = leia / "sections" / f"{sec['number']:02d}-{slug(sec['title'], 40)}"
        index.append(f"\n## {sec['title']}\n")
        li = soup.select_one(f"li#section-{sec['number']}")
        summary = li and li.select_one(".summarytext")
        if summary and summary.find_parent("li", class_="section") is li:
            md, _ = m.to_markdown(summary, course_url, sec_dir / "_summary.md", sec_dir / "_summary")
            if md:
                m.write(sec_dir / "_summary.md", f"# {sec['title']}\n\n{md}")
                index.append(f"- [section summary]({os.path.relpath(sec_dir / '_summary.md', leia)})")
        for pos, cmid in enumerate(map(int, sec["cmlist"]), 1):
            cm = cms.get(cmid)
            if not cm or cm.get("module") == "subsection":  # its modules are listed under their own section
                continue
            md_path = sec_dir / f"{pos:02d}-{slug(cm['name'], 50)}.md"
            m.cm_paths[cmid] = md_path
            kind = cm.get("module", "?")
            header = f"Type: {kind} ({cm.get('modname', '')})\n\n"
            # Intro shown on the course page; for some modules it is the only real content.
            intro = soup.select_one(f"#module-{cmid} .activity-altcontent")
            intro_md = m.to_markdown(intro, course_url, md_path, md_path.with_suffix(""))[0] if intro else ""
            if intro_md:
                header += f"## On the course page\n\n{intro_md}\n\n## Activity page\n\n"
            url = cm.get("url")
            if kind == "book":  # the print view has every chapter on one page
                url = f"{c.base}/mod/book/tool/print/index.php?id={cmid}"
            if url and cm.get("uservisible", True) and kind not in SKIP_MODULES:
                body = m.page(url, md_path, cm["name"], header)
            else:
                body = ""
                m.write(md_path, f"# {cm['name']}\n\n{header}Source: <{course_url}#module-{cmid}>\n")
            link = os.path.relpath(md_path, leia)
            index.append(f"- [{cm['name']}]({link}) ({kind})")
            if kind in ("assign", "quiz"):
                tasks.append((cmid, kind, cm["name"], link, body))

    r, final = c.get(f"/grade/report/user/index.php?id={course['id']}")
    table = BeautifulSoup(r.text, "html.parser").select_one("table.user-grade") if r is not None else None
    if table:
        m.write(leia / "grades.md", f"# Grades - {course['fullname']}\n\nSource: <{final}>\n\n" + m.to_markdown(table, final, leia / "grades.md", leia / "grades")[0])

    head = f"# {course['fullname']}\n\n- LeIA: <{course_url}>\n- Code: {course['shortname']}\n- Category: {course.get('coursecategory', '')}\n"
    head += "- Everything in this `leia/` folder is overwritten by the sync. Put your own files in `../work/`.\n"
    head += "- [tasks.md](tasks.md): assignments and tests. [grades.md](grades.md): grades.\n"
    m.write(leia / "README.md", head + "\n".join(index))
    return leia, tasks


STATUS_ROWS = re.compile(r"Status przesłanego|Stan oceniania|Submission status|Grading status|^\s*\|?\s*Ocena|Dozwolone próby|Attempts allowed|Limit czasu|Time limit|Metoda oceniania|Grading method", re.I)


def write_tasks(m, leia, tasks, events):
    lines = ["# Tasks and tests\n", "Deadlines come from the LeIA calendar; status lines are copied from each activity page.\n"]
    for cmid, kind, name, link, body in tasks:
        lines.append(f"## [{name}]({link}) ({kind})\n")
        for e in sorted((e for e in events if e["cmid"] == cmid), key=lambda e: e["time"]):
            lines.append(f"- **{e['when']}**: {e['name']}")
        lines += [f"- {' '.join(l.strip('| ').split('|')[:2]).strip()}" for l in body.splitlines() if l.startswith("|") and STATUS_ROWS.search(l) and not VOLATILE.search(l)]
        lines.append("")
    m.write(leia / "tasks.md", "\n".join(lines))


def fetch_events(c, courses):
    """Every calendar event from two months back to the last course end (at most 18 months ahead)."""
    now = datetime.now()
    end = max([datetime.fromtimestamp(x["enddate"]) for x in courses if x.get("enddate")] + [now])
    months = min((end.year - now.year) * 12 + end.month - now.month + 1, 18)
    events = {}
    for i in range(-2, months + 1):
        y, mo = divmod(now.year * 12 + now.month - 1 + i, 12)
        view = c.ajax("core_calendar_get_calendar_monthly_view", {"year": y, "month": mo + 1})
        for week in view["weeks"]:
            for day in week["days"]:
                for e in day["events"]:
                    cmid = re.search(r"/mod/\w+/view\.php\?id=(\d+)", e.get("url") or "")
                    events[e["id"]] = {
                        "time": e["timestart"],
                        "when": datetime.fromtimestamp(e["timestart"]).strftime("%Y-%m-%d %a %H:%M"),
                        "name": e["name"],
                        "kind": e.get("modulename") or e.get("eventtype") or "",
                        "course": (e.get("course") or {}).get("fullname", "site-wide"),
                        "cmid": int(cmid[1]) if cmid else None,
                        "url": e.get("url") or "",
                    }
    return sorted(events.values(), key=lambda e: e["time"])


def write_timeline(m, events):
    def rows(evs):
        out = ["| When | Course | What | Type | Local file |", "|---|---|---|---|---|"]
        for e in evs:
            p = m.cm_paths.get(e["cmid"])
            local = f"[open]({m.rel(p).replace(' ', '%20')})" if p else f"[LeIA]({e['url']})"
            out.append(f"| {e['when']} | {e['course']} | {e['name']} | {e['kind']} | {local} |")
        return out

    today = datetime.now().replace(hour=0, minute=0, second=0).timestamp()
    upcoming = [e for e in events if e["time"] >= today]
    past = [e for e in events if e["time"] < today][::-1]
    m.write(m.root / "TIMELINE.md", "\n".join(["# Timeline\n", "Generated by leia-sync from the LeIA calendar. Do not edit.\n", "## Upcoming\n", *rows(upcoming), "\n## Past\n", *rows(past)]))


def write_notifications(m):
    data = m.c.ajax("message_popup_get_popup_notifications", {"useridto": 0, "limit": 200})
    seen, new, out = set(m.state["notifications"]), [], ["# Notifications\n", "Newest first. Reading them here does not mark them as read on LeIA.\n"]
    for n in data["notifications"]:
        when = datetime.fromtimestamp(n["timecreated"]).strftime("%Y-%m-%d %H:%M")
        if n["id"] not in seen:
            new.append(f"{when} {n['subject']}")
        text = markdownify(n.get("fullmessagehtml") or n.get("fullmessage") or "", heading_style="ATX").strip()
        out.append(f"## {when} - {n['subject']}\n\n{n.get('contexturl') or ''}\n\n{re.sub(r'\n{3,}', '\n\n', text)}\n")
    m.state["notifications"] = sorted(seen | {n["id"] for n in data["notifications"]})
    m.written.add(m.root / "NOTIFICATIONS.md")
    (m.root / "NOTIFICATIONS.md").write_text("\n".join(out))
    return new


def write_changelog(m, new_notifications, errors):
    groups = {k: [p for kind, p in m.changes if kind == k and p not in ("TIMELINE.md",)] for k in ("new", "changed", "removed")}
    entry = [f"## {datetime.now():%Y-%m-%d %H:%M}\n"]
    for k, paths in groups.items():
        entry += [f"### {k.capitalize()} ({len(paths)})\n", *[f"- {p}" for p in paths], ""] if paths else []
    if new_notifications:
        entry += [f"### New notifications ({len(new_notifications)})\n", *[f"- {n}" for n in new_notifications], ""]
    if errors:
        entry += ["### Errors\n", *[f"- {e}" for e in errors], ""]
    if len(entry) == 1:
        entry.append("No changes.\n")
    path = m.root / "CHANGELOG.md"
    title = "# Changelog\n\nNewest first. Written by leia-sync.\n\n"
    old = path.read_text().removeprefix(title) if path.exists() else ""
    path.write_text(title + "\n".join(entry) + "\n" + old)
    return "\n".join(entry)


def password_for(cfg):
    if os.environ.get("LEIA_PASSWORD"):
        return os.environ["LEIA_PASSWORD"]
    r = subprocess.run(["security", "find-generic-password", "-s", cfg["keychain_service"], "-a", cfg["username"], "-w"], capture_output=True, text=True)
    if r.returncode:
        sys.exit("No password: add the Keychain entry or set LEIA_PASSWORD (see README.md).")
    return r.stdout.rstrip("\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selftest", action="store_true", help="check the safety rules and semester logic, no network")
    ap.add_argument("--course", type=int, help="sync only this course id (debugging)")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    cfg = {"base_url": "https://leia.pw.edu.pl", "cas_host": "cas.usos.pw.edu.pl", "keychain_service": "leia-sync", "data_dir": "..", "max_file_mb": 300}
    cfg |= tomllib.loads((REPO / "config.toml").read_text())
    root = (REPO / cfg["data_dir"]).resolve()
    state_path = root / ".leia-sync-state.json"
    state = {"files": {}, "notifications": [], "courses": {}, "first_term": None}
    state |= json.loads(state_path.read_text()) if state_path.exists() else {}

    c = Client(cfg["base_url"])
    c.login(cfg["username"], password_for(cfg), cfg["cas_host"])
    m = Mirror(c, root, state, cfg["max_file_mb"])

    courses = c.ajax("core_course_get_enrolled_courses_by_timeline_classification", {"offset": 0, "limit": 0, "classification": "all", "sort": "fullname"})["courses"]
    terms = [t for x in courses if (t := term_index(x["shortname"]) or term_index(x["fullname"]))]
    first = term_index(cfg.get("first_term")) or state["first_term"] or min(terms, default=0)
    state["first_term"] = first

    errors, synced = [], []
    for course in courses:
        if args.course and course["id"] != args.course:
            continue
        # Folder is fixed on first sight so a renamed course keeps its work/ folder.
        folder = state["courses"].setdefault(str(course["id"]), f"semestr-{semester_number(course['shortname'], course['fullname'], first)}/{slug(TERM.sub('', course['fullname']))}")
        try:
            synced.append((*sync_course(m, course, root / folder), course))
        except Exception as e:  # one broken course must not stop the others
            errors.append(f"{course['fullname']}: {type(e).__name__}: {e}")

    events = fetch_events(c, courses)
    for leia, tasks, _ in synced:
        write_tasks(m, leia, tasks, events)
        m.prune(leia)
    write_timeline(m, events)
    new_notifications = write_notifications(m)
    if not args.course:
        rows = [f"- **{x['fullname']}** ({x['shortname']}): `{m.rel(leia.parent)}/`, [index]({m.rel(leia)}/README.md), [tasks]({m.rel(leia)}/tasks.md)" for leia, _, x in synced]
        (root / "CLAUDE.md").write_text((REPO / "templates" / "CLAUDE.md").read_text().replace("{{COURSES}}", "\n".join(rows)))
    print(write_changelog(m, new_notifications, errors))
    state_path.write_text(json.dumps(state, indent=1))
    sys.exit(1 if errors else 0)


def selftest():
    for bad in (
        "https://x/mod/quiz/startattempt.php?cmid=1",
        "https://x/mod/quiz/attempt.php?attempt=1",
        "https://x/mod/quiz/processattempt.php",
        "https://x/mod/assign/view.php?id=1&action=editsubmission",
        "https://x/mod/bigbluebuttonbn/bbb_view.php?action=join&id=1",
        "https://x/login/logout.php?sesskey=abc",
        "https://x/course/view.php?id=1&sesskey=abc",
        "https://x/mod/forum/post.php?reply=1",
        "https://x/enrol/index.php?id=5",
    ):
        assert DENY.search(bad), bad
    for ok in (
        "https://x/mod/quiz/view.php?id=1",
        "https://x/mod/quiz/review.php?attempt=3&cmid=1",
        "https://x/mod/assign/view.php?id=1",
        "https://x/mod/forum/discuss.php?d=88",
        "https://x/pluginfile.php/1/mod_resource/content/1/a.pdf",
    ):
        assert not DENY.search(ok), ok
    assert CHILD.search("https://x/mod/forum/discuss.php?d=88") and not CHILD.search("https://x/mod/quiz/attempt.php?attempt=1")
    assert "core_message_send_instant_messages" not in AJAX_READ_ONLY
    assert term_index("639B-INxxx-MEP-ZBDUZ#2026Z") > term_index("X#2026L") and term_index("X#2027L") == term_index("X#2026Z") + 1
    first = term_index("2026Z")
    assert semester_number("A#2026Z", "", first) == 1 and semester_number("A#2027L", "", first) == 2 and semester_number("A#2027Z", "", first) == 3
    assert semester_number("Informacje i ogłoszenia", "Informacje i ogłoszenia", first) == 1
    assert slug("Zaawansowane bazy danych (2026Z)") == "zaawansowane-bazy-danych-2026z" and slug("Łódź żółć") == "lodz-zolc"
    print("selftest ok")


if __name__ == "__main__":
    main()
