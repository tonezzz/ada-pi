#!/usr/bin/env python3
"""Targeted augmentation for the confirm-gate corpus — fills classes that
are sparse in real traffic and caused recent failures:

  pos  Thai standalone/leading affirmations the regex knows but the
       corpus barely covers (ตกลง, เอาเลย, จัดไป, เผยแพร่เลย, ...)
  pos  EN/TH leading affirmation + instruction tail ("Yes, do the split")
  neg  Thai imperatives whose affirmation token is incidental
       (ตัวต่อไป "the next one" -> regex sees ต่อไป and false-fires)
  neg  Thai/EN negations and plain device commands (no affirmation)
  neg  EN imperatives containing an approval word (embedded)

Rows are marked src="aug" so they can be filtered out of evaluation or
ablation. Output: augment.jsonl {text, label, src}.
"""
import itertools, json, random, sys
from pathlib import Path

random.seed(7)
rows = []

def add(text, label):
    text = " ".join(text.split())
    if text:
        rows.append({"text": text, "label": bool(label), "src": "aug"})

# --- 1. standalone / leading Thai affirmations (pos) -----------------------
th_affirm = ["ตกลง", "เอาเลย", "เอาสิ", "จัดไป", "จัดเลย", "ทำเลย", "ได้เลย",
             "ส่งเลย", "เผยแพร่เลย", "ไปเลย", "เริ่มเลย", "ยืนยัน", "ยืนยันเลย",
             "ใช่", "เออ", "อือ", "อืม", "โอเค", "ออเค", "ดำเนินการเลย",
             "ให้ดำเนินการได้เลย", "เอาไปเลย", "ทำไปเลย", "อนุมัติ", "เห็นด้วย"]
th_prefix = ["", "อะ", "อ่า", "อ้า", "เออ", "ค่ะ", "ครับ", "อืม"]
th_suffix = ["", "ครับ", "ค่ะ", "ครับผม", "นะ", "จ้า", "หน่อย", "เลย", "ดิ"]
standalone = [f"{pre} {base} {suf}"
              for base, pre, suf in itertools.product(th_affirm, th_prefix, th_suffix)]
for t in random.sample(standalone, 140):
    add(t, True)
# bare standalone forms are the highest-frequency real turns — repeat to
# outweigh thousands of imperative negatives sharing the same tokens
for a in th_affirm:
    add(a, True); add(a, True); add(a, True)

# imperative-mood affirmation: command verb fused with เลย/ไปเลย/ได้เลย —
# "ปิดไปเลย" after a pending ask means "yes, close it". Sparse in the live
# corpus because the student diverged on these and they were dropped.
th_verbs = ["ปิด", "เปิด", "ส่ง", "ลบ", "รีเซ็ต", "รีสตาร์ท", "อัพเดท",
            "อัพเดต", "เปลี่ยน", "สลับ", "โชว์", "เล่น", "เผยแพร่", "บันทึก",
            "สร้าง", "แก้", "ย้าย", "หมุน", "ดำเนินการ", "เริ่ม"]
th_go = ["เลย", "ไปเลย", "ได้เลย", "ไป", "เอาเลย"]
verb_affirm = [f"{v}{g}" for v, g in itertools.product(th_verbs, th_go)]
verb_affirm += [f"{pre} {v}{g} {suf}" for v, g, pre, suf in
                itertools.product(th_verbs[:12], ["ไปเลย", "เลย", "ได้เลย"],
                                  ["อะ", "อ่า", "เออ", "ค่ะ"], ["ครับ", "ค่ะ", ""])]
for t in verb_affirm:
    add(t, True)
# two-part imperative affirmations: "Xไปเลยแล้ว Y" / "ให้ X ได้เลย"
verb_chain = [f"{v}ไปเลยแล้ว{w}" for v, w in
              itertools.product(["ปิด", "ลบ", "ส่ง", "เปลี่ยน", "รีเซ็ต"],
                                ["ส่ง GEV มาแทน", "เปิดอันใหม่", "อัพเดทเลย", "บันทึกเลย"])]
verb_chain += [f"ให้ {v} ได้ เลย" for v in th_verbs]
verb_chain += [f"ให้{v}ได้เลย" for v in th_verbs]
for t in verb_chain:
    add(t, True)

# ASR spacing variants — the transcriber splits "ปิดไปเลย" into "ปิด ไป เลย"
# and inserts particles ("เล่น ทับ ไป เลย"); real misses show both
spaced = []
for v in th_verbs:
    for g in ["ไป เลย", "ได้ เลย", "เลย"]:
        spaced.append(f"{v} {g}")
        spaced.append(f"{v} {g} {v} {g}")          # repeated utterance style
th_mid = ["ทับ", "ก่อน", "ดู", "ลอง", "ขึ้น", "มา"]
for v, m in itertools.product(th_verbs[:10], th_mid):
    spaced.append(f"{v} {m} ไป เลย")
    spaced.append(f"{v} {m} เลย")
for t in random.sample(spaced, 90):
    add(t, True)

# long EN leading-affirmations with compound instruction tails
en_tails_l = ["do the split", "send the report", "move the wall",
              "restart it", "update the page", "delete the old one"]
en_tails2 = ["put it on the left pane", "load it on the right pane",
             "set it up", "split the screen", "tell KK", "publish both"]
lead_long = []
for a, t1, t2 in itertools.product(["Yes", "Yes —", "Yes,", "Okay", "Sure —"],
                                   en_tails_l, en_tails2):
    lead_long.append(f"{a} {t1} and {t2}")
    lead_long.append(f"{a} {t1} — {t2}")
for t in random.sample(lead_long, 50):
    add(t, True)

# ตกลง family — real turns still under-score; extra reps + interrogative
# negatives ("ตกลงไหม/ตกลงหรือยัง" are questions, not affirmations)
toklom = ["ตกลง", "ตกลงครับ", "ตกลงค่ะ", "ตกลง ครับ", "ตกลง ค่ะ", "อะตกลง",
          "อ่ะตกลง", "อะ ตกลง", "อ่ะ ตกลง", "เออ ตกลง", "โอเค ตกลง",
          "ตกลงจ้า", "ตกลงนะ", "อืม ตกลง", "ตกลง ตกลง"]
for t in toklom:
    add(t, True); add(t, True)
for q in ["ตกลงไหม", "ตกลงมั้ย", "ตกลงหรือยัง", "ตกลงได้ไหม", "ตกลงดีไหม",
          "บอกว่าตกลงหรือยัง", "เขาตกลงไหม", "เคยตกลงกันไหม"]:
    add(q, False)

# leading affirmation + short instruction tail (the "Yes, do X" class)
th_tails = ["ส่งรายงาน", "ปิดกล้อง", "เปิดวิดีโอ", "บันทึกเลย", "ส่งไปที่จอ 1",
            "อัพเดทเลย", "ทำต่อเลย", "แจ้งคุณเคเค", "ลบอันเก่า", "เริ่มใหม่เลย"]
en_affirm = ["yes", "yes please", "go ahead", "ok", "okay", "sure", "confirm",
             "confirmed", "do it", "proceed", "sounds good", "yep"]
en_tails = ["publish it", "do the split", "send the report", "move the wall",
            "restart it", "update the page", "delete the old one",
            "save it to memory", "show it on screen 1", "switch it over"]
lead_th = [f"{a} {t}" for a, t in itertools.product(th_affirm[:14], th_tails)]
lead_en = [f"{a}, {t}" for a, t in itertools.product(en_affirm, en_tails)]
for t in random.sample(lead_th, 60) + random.sample(lead_en, 60):
    add(t, True)

# bare EN affirmations — corpus had "go ahead" only inside longer tails
en_standalone = ["yes", "yeah", "yep", "yup", "ok", "okay", "sure", "go ahead",
                 "do it", "proceed", "sounds good", "confirm", "confirmed",
                 "absolutely", "mhm", "approved", "yes please", "ok go ahead",
                 "yes do it", "sure go ahead", "yes go ahead", "okay go ahead",
                 "please do", "go for it"]
en_endings = ["", ".", " please", " please.", " — go ahead.", ", thanks",
              ", thanks.", " — do it."]
en_bare = [a + e for a, e in itertools.product(en_standalone, en_endings)]
for t in random.sample(en_bare, 70):
    add(t, True)

# --- 2. incidental-token imperatives (neg) ---------------------------------
# ต่อไป embedded in "ตัวต่อไป / รอบต่อไป / เพลงต่อไป" = "next", not "go ahead"
next_nouns = ["ตัวต่อไป", "รอบต่อไป", "เพลงต่อไป", "คลิปต่อไป", "อันต่อไป",
              "หน้าต่อไป", "ข้อต่อไป", "กล้องตัวต่อไป", "วิดีโอตัวต่อไป"]
th_cmd = ["ส่ง {} มาที่จอ 1", "เปิด {}", "เล่น {}", "ข้ามไป {}", "ดู {}",
          "เอา {} ขึ้นวอลล์", "โชว์ {}", "สลับไป {}", "หมุนไป {}"]
incidental = [cmd.format(noun) for noun, cmd in itertools.product(next_nouns, th_cmd)]
for t in random.sample(incidental, 50):
    add(t, False)
add("ส่ง Camwall ตัวต่อไปมาที่ทีวี", False)
add("ข้ามไปเพลงต่อไป", False)

# --- 3. negations + bare commands (neg) ------------------------------------
neg_phrases = ["ไม่ใช่", "ไม่ ใช่", "ไม่เอา", "ไม่เอาครับ", "อย่าเพิ่ง", "เดี๋ยวก่อน",
               "ไม่ต้อง", "ยังไม่ต้อง", "no", "nope", "not now", "wait",
               "hold on", "don't"]
neg_obj = ["", "อันนั้น", "กล้องตัวนั้น", "จอ 2", "ที่บ้านผม", "ของคุณเคเค",
           "the wall", "the report", "that one"]
negations = [f"{n} {o}" for n, o in itertools.product(neg_phrases, neg_obj)]
for t in random.sample(negations, 60):
    add(t, False)

# plain device/media commands with no affirmation anywhere
# memory-write requests are imperatives, NOT answers to a pending confirm —
# golden case "save this to memory please" must stay negative
mem_req = ["save this to memory please", "save it to memory",
           "remember this", "remember that", "please remember this",
           "save to memory: {}", "remember that {}", "save this: {}",
           "note that {}", "จดไว้ {}", "จำไว้นะ {}", "จำไว้ {}", "บันทึกไว้ {}"]
mem_obj = ["the wifi password is on the fridge",
           "the repairman comes Friday", "KK likes spicy food",
           "the lab stack design is approved", "the gate code changed"]
for t in mem_req:
    if "{}" in t:
        for o in mem_obj:
            add(t.format(o), False)
    else:
        add(t, False)

plain_cmds = ["เปิดไฟห้องนั่งเล่น", "ปิดทีวี", "เปิดแอร์", "ส่งวิดีโอวอลล์ขึ้นจอ",
              "เปิดวอลเปเปอร์", "หมุนกล้องไปทางซ้าย", "อ่านรายงานหน่อย",
              "สรุปข่าววันนี้", "วางแผนให้หน่อย", "ออกทริปเลย",
              "turn off the TV", "show the pool camera", "dim the lights",
              "play the flood report", "what's the weather",
              "remember the wifi password", "check the fence line"]
for c in plain_cmds:
    add(c, False)

# embedded approval words inside a longer request (neg) — the regex trap
embedded_neg = ["confirm the camera moved first",
                "confirm the deployment is done",
                "confirm that the backup ran",
                "verify the config then confirm it",
                "confirm the page before deleting",
                "check and confirm the wall is up",
                "make sure it's ok to proceed",
                "confirm receipt of the report",
                "check if the config is ok",
                "the deploy is approved — now write the runbook",
                "tell me again that it's ok to proceed",
                "is it okay to restart the service",
                "remember that the design is approved",
                "delete the page but confirm with KK first",
                "turn off the TV ok",
                "ส่งรายงานแล้วบอกว่าโอเคไหม",
                "เช็คก่อนว่าใช่ตัวที่ต้องการไหม",
                "บอกคุณเคเคว่าตกลงหรือยัง"]
for c in embedded_neg:
    add(c, False)

seen = set()
out = []
for r in rows:
    k = r["text"].strip().lower()
    if k in seen:
        continue
    seen.add(k)
    out.append(r)
path = Path(sys.argv[1] if len(sys.argv) > 1 else "augment.jsonl")
path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out))
print(f"{len(out)} augmented rows -> {path} "
      f"({sum(1 for r in out if r['label'])} pos / {sum(1 for r in out if not r['label'])} neg)")
