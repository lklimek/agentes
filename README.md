# Agentes

> *Agentes in Rebus* — secret agents of the late Roman Empire, dispatched across
> provinces to gather intelligence and execute imperial orders.

A Claude Code plugin marketplace shipping AI agent plugins for blockchain
developers and productive dev workflows.

## Plugins

| Plugin | Description | Category |
|--------|-------------|----------|
| [claudash](https://github.com/lklimek/claudash) | Dash Platform development skills with hybrid lexicon-based documentation lookup | Blockchain |
| [claudius](https://github.com/lklimek/claudius) | Opinionated dev lifecycle toolkit with agents and skills | Productivity |

## Installation

```bash
# Add marketplace
/plugin marketplace add lklimek/agentes

# Browse available plugins
/plugin

# Install individual plugins
/plugin install claudash@agentes
/plugin install claudius@agentes
```

## For project teams

Auto-install the marketplace when team members open your repository by adding
to `.claude/settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "agentes": {
      "source": {
        "source": "github",
        "repo": "lklimek/agentes"
      }
    }
  }
}
```

## Updating

```bash
/plugin marketplace update
```

Plugins are fetched from their source repositories. Updating the marketplace
refreshes the catalog; individual plugins update independently.

## Architecture

```
lklimek/agentes          ← this repo (marketplace catalog)
  ├── claudash           → lklimek/claudash (Dash Platform skills)
  └── claudius           → lklimek/claudius (dev lifecycle toolkit)
```

Each plugin lives in its own repository with independent versioning. This
marketplace is a lightweight catalog pointing to them — no plugin code lives
here.

## Contributing

Want to add a plugin to this marketplace? Open a PR adding an entry to
`.claude-plugin/marketplace.json`.

## License

GPL-3.0. See [LICENSE](LICENSE).
