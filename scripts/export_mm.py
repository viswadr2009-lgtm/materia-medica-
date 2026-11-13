import json, os, re, html, urllib.request, psycopg2
from urllib.parse import urljoin
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
  s = re.sub(r"(?i)<b[^>]*>\s*([^<]{2,45})\s*</b>", r"\n###\1\n", s)
  s = re.sub(r"(?i)<br\s*/?>", "\n", s)
  s = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", s)
  s = re.sub(r"<[^>]+>", " ", s)
  s = html.unescape(s)
  lines = [re.sub(r"\s+", " ", ln).strip() for ln in s.splitlines()]
  return "\n".join(ln for ln in lines if ln)

def scrape_allen():
  base_url = "http://www.homeoint.org/books/allkeyn/"
  idx_url = base_url + "index.htm"
  try:
    idx_html = fetch_url(idx_url)
  except Exception as e:
    print("Failed Allen index:", e)
    return
  links = re.findall(r'<a[^>]+href=["\']?([^"\'\s>#]+\.htm)(?:#[^"\'\s>]*)?["\']?[^>]*>', idx_html, re.I)
  urls = []
  seen = set()
  for href in links:
    u = urljoin(idx_url, href)
    fn = u.split("/")[-1].lower()
    if u.startswith(base_url) and fn not in ("index.htm", "preface.htm", "intro.htm") and u not in seen:
      seen.add(u)
      urls.append(u)

  def load_page(u):
    try:
      page = fetch_url(u)
      body_m = re.search(r"(?is)<body[^>]*>(.*)</body>", page)
      text = clean_html(body_m.group(1) if body_m else page)
      blocks = re.split(r"(?im)^(?:###\s*)?Keynotes by H\.C\. Allen\s*$", text)
      out = []
      for blk in blocks:
        lines = [ln.lstrip("#").strip() for ln in blk.splitlines() if ln.strip()]
        lines = [ln for ln in lines if not any(x in ln.lower() for x in (
          "copyright ©", "homéopathe international", "presented by", "médi-t",
          "h.i. english", "h.i. français", "buy a copy"
        ))]
        if len(lines) < 2:
          continue
        name = lines[0].rstrip(".:").strip()
        if len(name) < 2 or len(name) > 55 or "preface" in name.lower():
          continue
        secs, buf, cur_h = [], [], "Keynotes"
        for ln in lines[1:]:
          m = re.match(r"^(Relations|Aggravation|Amelioration)\s*[.:-]?\s*(.*)$", ln, re.I)
          if m:
            if buf:
              secs.append({"h": cur_h, "t": "\n".join(buf)})
              buf = []
            cur_h = m.group(1).title()
            if m.group(2):
              buf.append(m.group(2))
          else:
            buf.append(ln)
        if buf:
          secs.append({"h": cur_h, "t": "\n".join(buf)})
        if secs:
          out.append({"id": 0, "a": "", "n": name, "h": name, "s": secs})
      return out
    except Exception:
      return []

  entries = []
  with ThreadPoolExecutor(max_workers=6) as ex:
    for part in ex.map(load_page, urls):
      entries.extend(part)
  entries.sort(key=lambda x: x["n"].lower())
  for i, e in enumerate(entries, start=1):
    e["id"] = 10000 + i
  if entries:
    with open("data/mm_allen.json", "w") as f:
      json.dump(entries, f, separators=(",", ":"))
    manifest.append({
      "abbrev": "allen", "title": "Allen's Keynotes", "author": "H.C. Allen",
      "lang": "en", "file": "mm_allen.json", "count": len(entries)
    })
    print(f"Exported Allen's Keynotes: {len(entries)} remedies")

def scrape_kent():
  base_url = "http://homeoint.org/books3/kentmm/"
  idx_url = base_url + "index.htm"
  try:
    idx_html = fetch_url(idx_url)
  except Exception as e:
    print("Failed Kent index:", e)
    return
  links = re.findall(r'<a[^>]+href=["\']?([^"\'\s>#]+\.htm)(?:#[^"\'\s>]*)?["\']?[^>]*>(.*?)</a>', idx_html, re.I | re.S)
  seen, targets = set(), []
  for href, label in links:
    full_url = urljoin(idx_url, href)
    fname = full_url.split("/")[-1].lower()
    if not full_url.startswith(base_url) or fname in ("index.htm", "preface.htm", "intro.htm", "author.htm") or full_url in seen:
      continue
    name = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html.unescape(label))).strip()
    if not name or name.lower() in ("main", "home", "buy a copy", "english", "français"):
      continue
    seen.add(full_url)
    targets.append((full_url, name))

  def load_one(item):
    url, link_name = item
    try:
      page = fetch_url(url)
      t_m = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
      page_title = html.unescape(t_m.group(1)).strip() if t_m else ""
      rem_name = page_title.split(" - ")[0].strip() if " - " in page_title else ""
      if not rem_name or rem_name.lower().startswith("lectures") or rem_name.isdigit():
        rem_name = link_name
      body_m = re.search(r"(?is)<body[^>]*>(.*)</body>", page)
      text = clean_html(body_m.group(1) if body_m else page)
      chunks = [c.strip() for c in text.split("\n") if len(c.strip()) > 2]
      filtered = [c for c in chunks if not any(x in c.lower() for x in (
        "copyright ©", "homéopathe international", "presented by", "médi-t",
        "h.i. english", "h.i. français", "buy a copy", "robert séror"
      ))]
      secs, buf, cur_h = [], [], "Overview"
      for line in filtered:
        if line.startswith("###"):
          cand = line[3:].strip(" .:-")
          if 2 <= len(cand) <= 45 and cand.lower() != rem_name.lower():
            if buf:
              secs.append({"h": cur_h, "t": "\n".join(buf)})
              buf = []
            cur_h = cand
          continue
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
      return {"id": 0, "a": "", "n": rem_name, "h": rem_name, "s": secs}
    except Exception:
      return None

  entries = []
  with ThreadPoolExecutor(max_workers=8) as ex:
    for res in ex.map(load_one, targets):
      if res:
        entries.append(res)
  entries.sort(key=lambda x: x["n"].lower())
  for i, e in enumerate(entries, start=1):
    e["id"] = 20000 + i
  if entries:
    with open("data/mm_kentmm.json", "w") as f:
      json.dump(entries, f, separators=(",", ":"))
    manifest.append({
      "abbrev": "kentmm", "title": "Kent's Lectures on MM", "author": "J.T. Kent",
      "lang": "en", "file": "mm_kentmm.json", "count": len(entries)
    })
    print(f"Exported Kent's Lectures: {len(entries)} remedies")

scrape_allen()
scrape_kent()

with open("data/mm_manifest.json", "w") as f:
  json.dump(manifest, f, separators=(",", ":"))
