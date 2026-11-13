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
print("Tables:", list(schema.keys()))

# Export remedies
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

# Detect Materia Medica tables
info_t = next((t for t in ["mminfo", "materiamedicainfo", "materiamedica"] if t in schema), None)
chap_t = next((t for t in ["mmchapter", "materiamedicachapter"] if t in schema), None)
sec_t = next((t for t in ["mmchaptersection", "mmsection", "materiamedicasection"] if t in schema), None)

manifest = []
if info_t and chap_t:
  info_cols = schema[info_t]
  chap_cols = schema[chap_t]
  sec_cols = schema.get(sec_t, []) if sec_t else []

  sec_by_chap = {}
  if sec_t:
    cur.execute(f"SELECT * FROM {sec_t}")
    for row in cur.fetchall():
      d = dict(zip(sec_cols, row))
      cid = d.get("mmchapter_id") or d.get("chapterid") or d.get("chapter_id")
      h = d.get("heading") or d.get("title") or ""
      t = d.get("content") or d.get("text") or d.get("body") or ""
      order = d.get("succ") or d.get("id") or 0
      if cid is not None and (h or t):
        sec_by_chap.setdefault(cid, []).append((order, h, t))

  cur.execute(f"SELECT * FROM {info_t}")
  books = [dict(zip(info_cols, r)) for r in cur.fetchall()]

  for b in books:
    ab = b.get("abbrev")
    if not ab:
      continue
    cur.execute(f"SELECT * FROM {chap_t} WHERE abbrev = %s", (ab,))
    crows = cur.fetchall()
    if not crows:
      continue
    entries = []
    for crow in crows:
      d = dict(zip(chap_cols, crow))
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
