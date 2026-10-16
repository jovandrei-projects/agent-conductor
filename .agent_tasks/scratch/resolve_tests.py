"""Resolve the interleaved test_conductor.py conflicts.

Conflict map (line numbers in the conflicted working file):
- C1 [9190..9840]: HEAD = ModalScrollStabilityTests + ModelComboboxTests
  head (ends at `def test_combobox_filter_sections_keyboard_and_persistence(self):`);
  BRANCH = ReviewWorkflowTests class (ends at
  `def test_js_review_badges_modal_and_ctx(self):` + docstring).
- shared head [9841..9845]: `js = ...` + `harness = r'''` + first two stub lines.
- C2 [9846..10513]: HEAD = combobox test harness body; BRANCH = review test harness body.
- shared tail [10514..10521]: `})().catch...` + `'''` + subprocess.run + assertEqual.
- C3 [10522..11833]: HEAD = FollowupDiscoveryTests + DeviceOwnershipTests +
  ExecutionContextLabelTests; BRANCH = empty.

Resolution: HEAD class block + shared head + HEAD harness + shared tail,
then BRANCH class block + shared head + BRANCH harness + shared tail, then
HEAD C3 content. Both test methods each get a full copy of the shared
method skeleton.
"""
import re
from pathlib import Path

p = Path("tests/test_conductor.py")
lines = p.read_text(encoding="utf-8").split("\n")

# Locate the three conflicts by marker lines
marks = []
for i, ln in enumerate(lines):
    if ln.startswith("<<<<<<< HEAD"):
        marks.append(("open", i))
    elif ln.startswith("======="):
        marks.append(("mid", i))
    elif ln.startswith(">>>>>>>"):
        marks.append(("close", i))

conflicts = []
i = 0
while i < len(marks):
    assert marks[i][0] == "open", marks[i]
    assert marks[i+1][0] == "mid", marks[i]
    assert marks[i+2][0] == "close", marks[i]
    conflicts.append((marks[i][1], marks[i+1][1], marks[i+2][1]))
    i += 3

assert len(conflicts) == 3, f"expected 3 conflicts, found {len(conflicts)}"

c1o, c1m, c1c = conflicts[0]
c2o, c2m, c2c = conflicts[1]
c3o, c3m, c3c = conflicts[2]

pre = lines[:c1o]                       # everything before conflict 1
c1_head = lines[c1o+1:c1m]              # ModalScrollStabilityTests + ModelComboboxTests head
c1_branch = lines[c1m+1:c1c]            # ReviewWorkflowTests + method head + docstring
shared_head = lines[c1c+1:c2o]          # js=... + harness = r''' + 2 stub lines
c2_head = lines[c2o+1:c2m]              # combobox harness body
c2_branch = lines[c2m+1:c2c]            # review harness body
shared_tail = lines[c2c+1:c3o]          # }).catch + ''' + subprocess + assertEqual
c3_head = lines[c3o+1:c3m]              # Followup/Device/ExecCtx classes
c3_branch = lines[c3m+1:c3c]            # empty
post = lines[c3c+1:]                    # if __name__ ...

assert not c3_branch or all(not x.strip() for x in c3_branch), "c3 branch side not empty?"

out = []
out += pre
# ModelComboboxTests.test_combobox... gets the shared head + HEAD harness + shared tail
out += c1_head
out += shared_head
out += c2_head
out += shared_tail
# ReviewWorkflowTests gets its method: shared head + BRANCH harness + shared tail
out += c1_branch
out += shared_head
out += c2_branch
out += shared_tail
# C3: keep HEAD classes
out += c3_head
out += post

new = "\n".join(out)
assert "<<<<<<<" not in new, "markers remain"
assert ">>>>>>>" not in new or "startswith((\"<<<<<<<\", \">>>>>>>\"))" in new

p.write_text(new, encoding="utf-8", newline="")
print("resolved; new length", len(new))
