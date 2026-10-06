# Changelog

## 0.2.1

- `gh_user` accepts Enterprise Managed User logins (`name_shortcode`), which were refused as "not a GitHub login".

## 0.2.0

- A repo the gh account cannot see is one error row naming the account, not a failed panel.
- Setting `gh_user`: the gh account this workspace's calls use (`GH_TOKEN` from `gh auth token -u`).
- Repos with another `git.repos.<name>.type` make no gh call.

## 0.1.0

- First version: GitHub pull requests and local git state per repo, Code reviews page, Today items, ticket Code panel, Rerun failed and Mark ready.
