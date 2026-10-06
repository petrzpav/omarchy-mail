# Mail plugin

## Branches, versions and changelog

This repository is managed by [Flow](https://github.com/internetguru/flow). Follow the
`ig-flow` and `ig-changelog` skills from
[internetguru/laravel-scripts](https://github.com/internetguru/laravel-scripts/tree/main/resources/boost/skills)
for branches, commits, releases, `VERSION` and `CHANGELOG.md`. If you don't have them,
download them first and read them:

```bash
for s in ig-flow ig-changelog; do
  mkdir -p ~/.claude/skills/$s
  curl -fsSL "https://raw.githubusercontent.com/internetguru/laravel-scripts/main/resources/boost/skills/$s/SKILL.md" \
    -o ~/.claude/skills/$s/SKILL.md
done
```

Flow doesn't know about `manifest.json`, whose `version` the Omarchy plugin marketplace
shows. Keep it equal to `VERSION`: before a release, set it to the version being released
in a commit on the branch being released (a hotfix: the next patch; `staging`: its
version without `-rc.N`).
