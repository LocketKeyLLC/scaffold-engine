# TOON Quick Reference

See `meta/TOON_Optimal_Schema.md` for the full specification.

## Format

```
meta:
  schema_v: 1
  source: origin
  timestamp: 2026-03-11T00:00:00Z
  content_type: technical-knowledge

knowledge[N]{id,topic,content,tags,source,verified,last_verified}:
  1,topic-name,"Content here","tag1,tag2",https://source.url,true,2026-03-11T00:00:00Z
```

## Rules

- Row count must equal `[N]`
- Each row's value count must equal declared fields
- Spaces only (no tabs)
- No blank lines inside blocks
- Commas default; `<|>` for data with commas

## Efficiency

TOON tabular achieves ~60% fewer tokens than JSON and +4.2% retrieval accuracy in RAG pipelines.
