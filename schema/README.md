# Claude Code Plugin Schemas

Reverse-engineered JSON Schemas for Claude Code's plugin system.

## Why these exist

Anthropic's `marketplace.json` files reference
`"$schema": "https://anthropic.com/claude-code/marketplace.schema.json"` but
that URL returns **404** — no standalone schema has ever been published.  The
same is true for `plugin.json`; there is no public schema for plugin
manifests either.

Without a published schema, marketplace maintainers and plugin authors have no
way to validate their configuration files offline.  These schemas fill that
gap.

## How they were produced

The actual validation logic lives as **Zod schemas** compiled into the
minified `cli.js` bundle of the
[`@anthropic-ai/claude-code`](https://www.npmjs.com/package/@anthropic-ai/claude-code)
npm package.  We extracted the schemas from **v2.1.61** of that package using
the following approach:

1. **Install the package** — `npm pack @anthropic-ai/claude-code` to get the
   tarball without running any install scripts.
2. **Locate the Zod definitions** — search `cli.js` for identifiers related to
   `pluginManifest`, `marketplacePlugin`, `author`, and `pluginSource`.
3. **De-minify and trace** — follow the `.extend()`, `.partial()`, and
   `.strict()` chains to reconstruct the full object shapes.
4. **Translate to JSON Schema** — map each Zod primitive, union, array, and
   refinement to its JSON Schema 2020-12 equivalent.
5. **Cross-reference** — validate the result against the
   [official docs](https://code.claude.com/docs/en/plugin-marketplaces),
   [plugins reference](https://code.claude.com/docs/en/plugins-reference),
   and live `marketplace.json` files from four Anthropic repositories
   (`anthropics/claude-code`, `anthropics/claude-plugins-official`,
   `anthropics/skills`, `anthropics/knowledge-work-plugins`).

### Key Zod structures

**Plugin manifest** (`plugin.json`):

```
coreFields.extend(
  hooks.partial(),
  commands.partial(),
  agents.partial(),
  skills.partial(),
  outputStyles.partial(),
  mcpServers.partial(),
  lspServers.partial(),
  settings.partial()
).strict()
```

16 fields total.  Only `name` is required.  `.strict()` means no additional
properties are allowed.

**Marketplace plugin entry** (each object in `marketplace.json` → `plugins`):

```
pluginManifest.partial().extend({
  name,       // re-declared as required
  source,     // required, discriminated union
  category,   // optional
  tags,       // optional
  strict,     // optional, default true
}).strict()
```

20 fields total (all 16 manifest fields made optional, plus 4
marketplace-only).  `name` and `source` are required.

## Files

| File | Validates | Fields |
|------|-----------|--------|
| `plugin.schema.json` | `.claude-plugin/plugin.json` | 16 (name required) |
| `marketplace.schema.json` | `.claude-plugin/marketplace.json` | top-level + 20 per plugin entry |

## Limitations

- **Zod refinements** (e.g. the reserved-name blocklist, path-traversal
  checks) cannot be expressed in JSON Schema and are not included.
- **Lazy/recursive types** (e.g. inline hook definitions) are approximated
  as `"type": "object"` rather than fully expanded.
- These schemas reflect v2.1.61 of `@anthropic-ai/claude-code`.  Fields may
  be added or changed in future versions.

## Updating

To refresh these schemas after a new Claude Code release:

1. `npm pack @anthropic-ai/claude-code` to get the latest tarball.
2. Search `cli.js` for the Zod schema variables (look for `.strict()` calls
   near plugin-related string literals).
3. Compare with the existing schemas and update as needed.
