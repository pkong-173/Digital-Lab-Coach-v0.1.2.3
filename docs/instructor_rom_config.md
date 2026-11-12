# Instructor guide: registering a lab file's ROM contents

One rule, for every lab file that contains a ROM: register the ROM's
official contents next to the file's official tests, and the tool treats
them as one more test. A student's ROM must hold exactly those words.
---

## 1. What students see

| Where | Effect |
|---|---|
| Layer 1 | Nothing is ever loaded into a ROM. An empty ROM keeps its warning, which says the lab registers the ROM's contents and the debugger refuses to run until the ROM matches. |
| Layer 3 Mode A | Before anything else, every file in the tree (the uploaded file and its subcircuits) whose filename has registered contents is checked word for word. Any empty, different or missing ROM: the board shows one fixed message naming the file and the ROM, how many words differ and the first differing address, and stops. No model call, no daily use consumed. All ROMs match: the analysis runs, the model is told the words are official, and any Data change it proposes on a ROM is stripped. On the coach's temp copy, words the coach appended after the registered ones are accepted. |
| Everywhere | The registered words never appear in the UI, the Settings page, `list_tests()`, the Mode A message, or the model payload. Words a student registered for their own rows (section 2) are theirs and are shown to them. |

The comparison ignores formatting: bare or upper-case hex, Digital's
`n*word` shorthand and trailing zero words all read the same. A student
ROM is read with its own `intFormat` attribute and compared as numbers.
The ROM checked is the one marked *Program Memory*; if none is marked,
every ROM in the file must hold the registered words.

A file with official tests but no registered ROM contents is not
checked; Mode A then treats its ROM like any other component.

## 2. A student's own rows

For instance, `cpu.dig` is marked `"editable": true`: students may add rows to its test
in Settings → Official tests. The built-in rows go with the registered
program, so the test is extended, never rewritten:

- The header and the built-in rows stay as they are (comments and spacing
  are free). A changed or removed built-in row is refused with its number.
- Every added row is saved with one instruction word: `0x` and up to 8
  hex digits, one per line, in row order. The box sits under the rows.
  The save is refused until the number of words equals the number of
  added rows, and the message gives both counts.
- From then on the ROM must hold the registered program followed by
  those words. The student enters them in the ROM in Digital; nothing is
  written into a student's file. Layer 1's empty-ROM note and the Mode A
  check keep working. The check tells three cases apart: the official
  part is wrong; the official part is right and the words after it do not
  match the registered ones; the official part is right and words follow
  that nobody registered (the message says how to register them).
- Saving the built-in rows with nothing added, or *Delete override*,
  brings the built-in test back.

*Adopt into official tests* stores the same pair from the circuit Mode B
verified: its rows, and the words its ROM holds after the registered
program. A row past the end of the ROM is stored with the zero word,
which is what it runs. When that circuit's ROM does not start with the
registered program, the rows are stored without words and the ROM is not
checked: they describe another program.

## 3. Where the configuration lives

One JSON file, shipped with the tool:

```
data/official_tests_defaults.json
```

One entry per lab **filename** (matching is by exact filename, e.g.
`cpu.dig`):

```json
{
  "romlab.dig": {
    "content": "A D\n0 5\n1 6",
    "sha1": "<normalized fingerprint of content>",
    "runtime": "<base64 blob holding the ROM words>"
  }
}
```

- `content` — the official testcase rows (Digital test format: first
  line is the signal header, then value rows). Injected into a run-scoped
  copy whenever a student file's own testcase is missing or modified.
- `sha1` — fingerprint used to recognize an unmodified official
  testcase inside a student file.
- `runtime` — the ROM contents. Present for every file that has a ROM.

The `runtime` key can only be configured here, in the shipped defaults
file.

## 4. Register a lab

1. Open your instructor copy of each lab file in Digital and make sure it
   holds the official testcase and the ROM filled with the official words
   (check *Program Memory* on the ROM if the file has several).
2. Generate and merge the entries in one command:

       uv run python -m dlc.fingerprint cpu.dig control-unit.dig cpu_new.dig controlunit.dig --with-rom --merge data/official_tests_defaults.json

   Each file gets `content`, `sha1` and, when it has a ROM, `runtime`.
   Existing entries are updated in place and everything else in the file
   is kept. A file without a testcase or with an empty ROM is skipped
   with a message.
3. Restart the server.

By hand instead: `content` is the testcase text, `sha1` comes from
`dlc.l3.manifest.normalized_test_hash(content)`, and `runtime` is
`base64(json.dumps({"rom": "<words>"}))` where `<words>` is the ROM's
`Data` attribute as Digital stores it (comma-separated, address 0 first,
bare hex, `7*1f` shorthand allowed). Base64 is obfuscation, not
encryption; keep answer `.dig` files out of the repository.

## 5. Verify

1. Upload a student-style file with the right filename and an empty ROM.
   Layer 1 must show the ROM warning with the registration note.
2. Open Layer 3 and click Analyze: the board must answer "ROM check: ROM
   is empty" and the daily-use counter must not move.
3. Re-upload with the words typed in and one word changed: the board
   names the first differing address. With the exact words the analysis
   runs.

## 6. Quick reference

| Piece | Where |
|---|---|
| The one file to edit | `data/official_tests_defaults.json` (entry per lab filename: `content`, `sha1`, `runtime`) |
| Generate entries | `python -m dlc.fingerprint <files> --with-rom --merge data/official_tests_defaults.json` |
| The check itself | `dlc/testing/inject.check_rom_contents` (Mode A gate) |
| A student's added rows and their words | `dlc/l3/official_store.save_test` (the rule), `~/.dlc/official_tests.json` (`program_extra`) |
| Everything else instructors can change | the README's *Where to change what* table |
