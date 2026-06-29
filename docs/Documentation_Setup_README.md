# Documentation Setup Guide

Use this README any time you need to rebuild or relocate the MkDocs-powered documentation for the Control Panel Command Station.

## 1. Keep Docs with the Control Panel Repo

1. Move `docs/`, `mkdocs.yml`, and `.github/workflows/mkdocs-deploy.yml` into the root of the Control Panel repository (`GUI_and_components/control_panel/`).
2. Commit them alongside application changes so doc updates and cockpit features stay in sync.
3. If the documentation must live in a separate repo, duplicate the files there and update `site_url`/`repo_url`, but expect extra coordination between code and docs.

## 2. Install Tooling Locally

```bash
pip install mkdocs mkdocs-material pymdown-extensions
```

- Use a dedicated virtual environment so `mkdocs` and its plugins stay isolated from the Dash app dependencies.
- Run `mkdocs --version` to confirm the CLI is on your `PATH`.

## 3. Authoring Workflow

1. Edit Markdown inside `docs/` (e.g., `DEMO_project_documentation.md`).
2. Preview with `mkdocs serve` → http://127.0.0.1:8000; the server reloads whenever files change.
3. Keep persona-specific callouts, env-var tables, and onboarding checklists close to the sections they support. Shared tables can be refactored into snippets via `pymdownx.snippets` if multiple pages need them.

## 4. Customize MkDocs Configuration

The root `mkdocs.yml` controls:

- **Site metadata**: update `site_name`, `site_description`, `site_url`, `repo_url` to match the Control Panel repository.
- **Navigation**: adjust the `nav:` tree to expose new guides, slide decks, or JSON quizzes.
- **Theme**: Material palette (`primary: brown`, `accent: amber/lime`) mirrors the retro console, but feel free to tweak under `theme.palette`.
- **Markdown extensions**: `pymdownx` features (tabs, admonitions, snippets) are already enabled; add more as needed.

After editing `mkdocs.yml`, rerun `mkdocs serve` to make sure navigation and theme changes render as expected.

## 5. GitHub Action Deployment

1. The workflow in `.github/workflows/mkdocs-deploy.yml` installs MkDocs, builds the site, and executes `mkdocs gh-deploy --force` on every push to `main` (manual dispatch is also enabled).
2. Ensure `permissions.contents` is set to `write` so the Action can push to `gh-pages`.
3. If your default branch differs, update the `branches` filter.

## 6. Enable GitHub Pages

1. Run `mkdocs gh-deploy` once locally to bootstrap the `gh-pages` branch.
2. In the repo’s **Settings → Pages**, choose:
   - **Source**: `Deploy from branch`
   - **Branch**: `gh-pages`
   - **Folder**: `/ (root)`
3. (Optional) Add a custom domain: commit a `CNAME` file to `gh-pages` and create matching DNS records.

## 7. Verification Checklist

- [ ] `mkdocs serve` renders without warnings.
- [ ] GitHub Actionssh-add ~/.ssh/id_ed25519 succeeds and updates `gh-pages`.
- [ ] Pages site loads (no 404) and shows the latest nav.
- [ ] README/Control Panel repo references the live docs URL.
- [ ] Quizzes JSON files are uploaded to the Learning Platform if needed.

## 8. When to Consider Docusaurus Instead

Stay on MkDocs while you only need Markdown SOPs, quick builds, and GitHub Pages hosting. Switch to Docusaurus if you require:

- MDX components with live React widgets.
- Built-in doc versioning and localization.
- Blog or marketing-style landing pages living beside technical docs.

Docusaurus introduces a Node.js/Yarn toolchain, so plan for that overhead before migrating.

---

With these steps in place, any contributor can spin up the docs site locally, publish changes through GitHub Actions, and keep the Control Panel knowledge base versioned right next to the code it describes.
