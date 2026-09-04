# Settings Storage Wording Design

## Goal

Make the two DoukHub storage settings distinguishable without changing storage or migration behavior.

## Design

- Rename the first settings card to `兜底配置文件`, and describe it as the import/fallback JSON file. State that normal settings live in the application database.
- Keep the `应用数据目录` card, but show the current directory above the migration form.
- Clarify that migration moves only the main database, history database, backups, and collection logs. It does not move the fallback config file or media files.
- Pass the current application data directory from the settings route to the template.

## Verification

Update the settings-page UI test to assert the new labels, the current-directory caption, and the fallback-config explanation.
