# Publishing this bundle to GitHub

This directory is already laid out as the repository root.

## Option A: Git command line

1. Create a new **empty** GitHub repository. Do not pre-create a README, license,
   or `.gitignore`; this bundle already contains them.
2. In this directory run:

```bash
git init
git add .
git commit -m "Initial public beta of ThirtyStash"
git branch -M main
git remote add origin https://github.com/YOUR-USER/ThirtyStash.git
git push -u origin main
```

3. In GitHub repository settings, optionally enable:
   - Issues
   - Discussions
   - Private vulnerability reporting
   - Dependabot alerts/security updates

Suggested repository description:

> Local-first, self-hosted preparedness inventory for 30 days of food and water plus household medical supplies.

Suggested topics:

`docker`, `self-hosted`, `preparedness`, `inventory`, `food-storage`,
`emergency-preparedness`, `sqlite`, `flask`, `barcode-scanner`,
`open-food-facts`

## Option B: GitHub web upload

Create an empty repository, choose **Add file → Upload files**, and upload the
contents of this directory, including dotfiles such as `.gitignore`, `.github`,
and `.env.example`.

## First release

A sensible first tag is:

```bash
git tag -a v1.2.0-beta.4 -m "ThirtyStash 1.2.0 public beta"
git push origin v1.2.0-beta.4
```

Use the `1.2.0-beta.4` section of `CHANGELOG.md` as the basis for the GitHub
release notes.
