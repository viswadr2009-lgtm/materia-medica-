import json, os, re, psycopg2

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
with open("data/schema.json", "w") as f:
  json.dump(schema, f, indent=2)
print("mminfo cols:", schema.get("mminfo"))
print("mmchapter cols:", schema.get("mmchapter"))
print("mmsection cols:", schema.get("mmsection"))

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
  ab = b.get("abbrev")
  bid = b.get("id")
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
      "id": cid,
      "a": rem.get("a", ""),
      "n": rem.get("n", "") or heading,
      "h": heading,
      "s": [{"h": h, "t": t} for _, h, t in secs]
    })
  if not entries:
    continue
  entries.sort(key=lambda x: (x["n"] or x["h"]).lower())
  safe_ab = re.sub(r"[^a-zA-Z0-9_-]", "_", ab)
  fname = f"mm_{safe_ab}.json"
  with open(f"data/{fname}", "w") as f:
    json.dump(entries, f, separators=(",", ":"))
  manifest.append({
    "abbrev": ab,
    "title": b.get("title") or b.get("displaytitle") or ab,
    "author": b.get("authorlastname") or b.get("author") or "",
    "lang": b.get("language") or "en",
    "file": fname,
    "count": len(entries)
  })
  print(f"Exported MM {ab}: {len(entries)} remedies")

with open("data/mm_manifest.json", "w") as f:
  json.dump(manifest, f, separators=(",", ":"))
