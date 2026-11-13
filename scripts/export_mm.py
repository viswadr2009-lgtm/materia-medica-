import json, os, re, html, urllib.request, psycopg2
from concurrent.futures import ThreadPoolExecutor

conn = psycopg2.connect(dbname="oorep", user="oorep_user", password="oorep", host="localhost")
cur = conn.cursor()

cur.execute("""
  SELECT table_name, column_name
  FROM information_schema.columns
  WHERE table_schema = 'public'
  ORDER BY table_name, ordinal_position
""")
schema = {}
for t, c in cur.fetchall():
  schema.setdefault(t, []).append(c)

os.makedirs("data", exist_ok=True)

remedies = {}
if "remedy" in schema:
  rem_cols = schema["remedy"]
  cur.execute("SELECT * FROM remedy")
  for row in cur.fetchall():
    d = dict(zip(rem_cols, row))
    rid = d.get("id")
    if rid is not None:
      remedies[rid] = {
        "a": d.get("nameabbrev") or d.get("abbrev") or "",
        "n": d.get("namelong") or d.get("name") or ""
      }
with open("data/remedies.json", "w") as f:
  json.dump(remedies, f, separators=(",", ":"))

info_cols = schema.get("mminfo", [])
chap_cols = schema.get("mmchapter", [])
sec_cols = schema.get("mmsection", [])

cur.execute("SELECT * FROM mminfo")
books = [dict(zip(info_cols, r)) for r in cur.fetchall()]
cur.execute("SELECT * FROM mmchapter")
all_chaps = [dict(zip(chap_cols, r)) for r in cur.fetchall()]
cur.execute("SELECT * FROM mmsection")
all_secs = [dict(zip(sec_cols, r)) for r in cur.fetchall()]

sec_by_chap = {}
for d in all_secs:
  cid = d.get("mmchapter_id") or d.get("chapter_id") or d.get("chapterid") or d.get("mmchapterid")
  h = d.get("heading") or d.get("title") or ""
  t = d.get("content") or d.get("text") or d.get("body") or ""
  order = d.get("succ") or d.get("seq") or d.get("id") or 0
  if cid is not None and (h or t):
    sec_by_chap.setdefault(cid, []).append((int(order) if str(order).isdigit() else 0, h, t))

manifest = []
for b in books:
  ab, bid = b.get("abbrev"), b.get("id")
  if not ab:
    continue
  entries = []
  for d in all_chaps:
    cab = d.get("abbrev") or d.get("mm_abbrev") or d.get("mminfo_abbrev")
    cbid = d.get("mminfo_id") or d.get("mm_id") or d.get("materiamedica_id")
    if not ((cab and cab == ab) or (bid is not None and cbid == bid)):
      continue
    cid = d.get("id")
    rid = d.get("remedy_id") or d.get("remedyid")
    rem = remedies.get(rid, {})
    heading = d.get("heading") or d.get("title") or rem.get("n") or rem.get("a") or f"Entry {cid}"
    secs = sec_by_chap.get(cid, [])
    secs.sort(key=lambda x: x[0])
    entries.append({
      "id": cid, "a": rem.get("a", ""), "n": rem.get("n", "") or heading,
      "h": heading, "s": [{"h": h, "t": t} for _, h, t in secs]
    })
  if not entries:
    continue
  entries.sort(key=lambda x: (x["n"] or x["h"]).lower())
  safe_ab = re.sub(r"[^a-zA-Z0-9_-]", "_", ab)
  fname = f"mm_{safe_ab}.json"
  with open(f"data/{fname}", "w") as f:
    json.dump(entries, f, separators=(",", ":"))
  manifest.append({
    "abbrev": ab, "title": b.get("title") or ab,
    "author": b.get("authorlastname") or "", "lang": b.get("language") or "en",
    "file": fname, "count": len(entries)
  })
  print(f"Exported OOREP {ab}: {len(entries)}")

def fetch_url(url):
  req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
  with urllib.request.urlopen(req, timeout=20) as r:
    raw = r.read()
    for enc in ("utf-8", "cp1252", "latin-1"):
      try:
        return raw.decode(enc)
      except Exception:
        pass
    return raw.decode("utf-8", errors="ignore")

def clean_html(s):
  s = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", s)
  s = re.sub(r"(?i)<br\s*/?>", "\n", s)
  s = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", s)
  s = re.sub(r"<[^>]+>", " ", s)
  s = html.unescape(s)
  lines = [re.sub(r"\s+", " ", ln).strip() for ln in s.splitlines()]
  return "\n".join(ln for ln in lines if ln)

def scrape_homeoint(base_url, abbrev, title, author, id_offset):
  try:
    idx_html = fetch_url(base_url + "index.htm")
  except Exception as e:
    print(f"Failed index {base_url}: {e}")
    return
  links = re.findall(r'href=["\']([a-zA-Z0-9_-]+\.htm)["\'][^>]*>(.*?)</a>', idx_html, re.I | re.S)
  seen, targets = set(), []
  for href, label in links:
    low = href.lower()
    if low in ("index.htm", "preface.htm", "intro.htm") or href in seen:
      continue
    name = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html.unescape(label))).strip()
    if not name or len(name) < 2:
      continue
    seen.add(href)
    targets.append((href, name))

  def load_one(item):
    href, name = item
    try:
      page = fetch_url(base_url + href)
      body_m = re.search(r"(?is)<body[^>]*>(.*)</body>", page)
      body = body_m.group(1) if body_m else page
      text = clean_html(body)
      chunks = [c.strip() for c in text.split("\n") if len(c.strip()) > 3]
      filtered = [c for c in chunks if not any(x in c.lower() for x in (
        "copyright ©", "homéopathe international", "presented by", "médi-t",
        "h.i. english", "h.i. français", "buy a copy", "main"
      ))]
      secs = []
      buf = []
      cur_h = "Overview"
      for line in filtered:
        if len(line) < 42 and (line.endswith(".") or line.endswith(":")) and line[0].isupper():
          if buf:
            secs.append({"h": cur_h, "t": "\n".join(buf)})
            buf = []
          cur_h = line.rstrip(".:")
        else:
          buf.append(line)
      if buf:
        secs.append({"h": cur_h, "t": "\n".join(buf)})
      if not secs:
        return None
      return {"id": 0, "a": "", "n": name, "h": name, "s": secs}
    except Exception:
      return None

  entries = []
  with ThreadPoolExecutor(max_workers=8) as ex:
    for res in ex.map(load_one, targets):
      if res:
        entries.append(res)
  entries.sort(key=lambda x: x["n"].lower())
  for i, e in enumerate(entries, start=1):
    e["id"] = id_offset + i
  if entries:
    fname = f"mm_{abbrev}.json"
    with open(f"data/{fname}", "w") as f:
      json.dump(entries, f, separators=(",", ":"))
    manifest.append({
      "abbrev": abbrev, "title": title, "author": author,
      "lang": "en", "file": fname, "count": len(entries)
    })
    print(f"Exported {title}: {len(entries)} remedies")

scrape_homeoint("http://www.homeoint.org/books/allkeyn/", "allen", "Allen's Keynotes", "H.C. Allen", 10000)
scrape_homeoint("http://homeoint.org/books3/kentmm/", "kentmm", "Kent's Lectures on MM", "J.T. Kent", 20000)

with open("data/mm_manifest.json", "w") as f:
  json.dump(manifest, f, separators=(",", ":"))
