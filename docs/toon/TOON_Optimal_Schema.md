# TOON Optimal Schema

> Token-Oriented Object Notation — maximum compression, maximum clarity, minimum noise.

---

## Identity

- **Extension:** `.toon`
- **Media Type:** `text/toon`
- **Indentation:** Spaces only (no tabs for byte/visual consistency)
- **Workflow:** JSON (storage) ↔ TOON (LLM boundary) ↔ JSON (processing)

---

## Core Principles

1. **Declare once, reference never.** Field names appear only in the header.
2. **Count everything.** `[N]` length markers prevent truncation and hallucinated rows.
3. **Flatten aggressively.** Key-fold nested paths into single-line declarations.
4. **Short keys.** `src` not `source_url`, `desc` not `description`. Drop redundant prefixes inside scoped objects.
5. **Delimiter-match your data.** Commas default; pipes for data containing commas; tabs for large uniform tables.
6. **Metadata first.** A `meta:` block before arrays gives the LLM context before it processes rows.
7. **Strict mode on.** Row count == `[N]`, value count == field count, consistent delimiters, no blank lines inside blocks.

---

## Syntax Reference

### Scalar

```
key: value
```

No quotes unless the value contains the active delimiter or a literal newline.

### Tabular Array (highest efficiency — use whenever data is uniform)

```
header[N]{field1,field2,field3}:
val1,val2,val3
val1,val2,val3
```

- Header declares name, count, fields, and delimiter in one line.
- Each subsequent line is pure data — zero repeated keys.

### Nested / Mixed Array (use only when rows differ in shape)

```
items[N]:
- scalar_value
- key1: val
  key2: val
```

YAML-like indentation, no braces, no quotes.

### Key Folding (deep nesting → flat line)

```
data.meta.items[N]{id,name,status}:
1,widget,active
2,gadget,retired
```

Entire path collapsed into the header. One line replaces multiple nesting levels.

### Pipe-Delimited Override (data contains commas)

```
contacts[2]<|>{name,address,phone}:
Alice Smith|123 Main St, Apt 4|555-0101
Bob Jones|789 Oak Ave, Suite 12|555-0202
```

`<|>` in the header signals the delimiter switch.

---

## Meta Block Pattern

Place before any data block to front-load context:

```
meta:
  schema_v: 1
  source: crm_export
  generated: 2026-03-11
  encoding: utf-8

accounts[3]{id,name,tier,active}:
101,Acme Corp,enterprise,true
102,Beta LLC,starter,false
103,Gamma Inc,pro,true
```

The LLM reads schema version, source, and timestamp *before* encountering a single data row — reducing misinterpretation.

---

## Strict-Mode Contract (enabled by default)

| Rule | Purpose |
|---|---|
| Row count == `[N]` | Detects truncation |
| Values per row == field count | Detects corruption |
| Delimiter consistent header↔rows | Prevents parse drift |
| Indent = exact multiples of indent size | Enforces structure |
| No blank lines inside blocks | Eliminates ambiguity |

Violations are structural errors, not warnings.

---

## Noise Reduction Checklist

- **Remove all quotes** unless escaping a delimiter or newline.
- **Remove all braces/brackets** from data rows — structure lives in the header.
- **Remove all repeated key names** — declared once, inferred per-row by position.
- **Remove all whitespace padding** — no trailing spaces, no alignment padding.
- **Remove all comments** — TOON has no comment syntax; metadata goes in `meta:`.
- **Collapse nesting** via key folding wherever data is uniform.
- **Use short keys** aligned to BPE tokenizer boundaries (leading-space words tokenize as single tokens).

---

## Format Efficiency Ranking (for reference)

```
TOON (tabular) > TSV/CSV > Markdown > YAML > minified JSON > formatted JSON > XML
```

TOON tabular achieves ~60% fewer tokens than equivalent JSON and +4.2% retrieval accuracy in RAG pipelines.

---

## Minimal Complete Example

```
meta:
  v: 1
  src: inventory_db
  ts: 2026-03-11

products[4]{id,name,cat,price,stock}:
1,Widget A,hardware,29.99,142
2,Widget B,hardware,49.99,87
3,Service X,software,9.99,null
4,Service Y,software,19.99,null

suppliers[2]<|>{id,name,addr,contact}:
1|Acme Parts|456 Industrial Blvd, Bay 3|[email protected]
2|Global Supply|789 Trade Rd, Unit 12|[email protected]
```

This encodes 6 fields × 4 rows + 4 fields × 2 rows with full metadata in ~40% of the tokens JSON would require.
