# Optional Buy Me a Coffee support CTA

The start screen has a button **About the game** inline with the
**Ready to play?** title, and a separate coffee support row under the intro.

The coffee button stays disabled until the owner has registered a personal
Buy Me a Coffee page. The app does **not** process payments, accept payment
information, or grant stars for donations.

## Enabling the CTA (after owner verifies profile and payouts)

Add exactly one variable in the running Quiz backend's private `.env`:

```dotenv
BUY_ME_A_COFFEE_URL=https://buymeacoffee.com/YOUR_PROFILE
```

Only HTTPS personal profile URLs under `buymeacoffee.com` or
`www.buymeacoffee.com` are accepted. Invalid or blank values return
no `donation_url` field in the existing `GET /api/client-config` and keep
the button disabled (the optional `donation_url` field is omitted from the client config). The frontend independently verifies the hostname and
single profile path. Configure an actual verified owner URL, never a guessed
user name or generic provider homepage.

After editing `.env`, recreate **only** the Quiz backend, then verify
`GET /api/client-config` returns the intended public URL. Do not print or
replace any other secrets. The external link uses
`target="_blank" rel="noopener noreferrer"`, so donation checkout remains
off-site. The frontend rebuild is required once to pick up the new markup,
but future donation URL changes are backend-config-only.

Texts come from `backend/localization/en.json` and the normal AI Localizer
dictionary pipeline. Check Russian and RTL layouts in the next browser E2E.

Do not deploy this feature to public production before the configured URL is
under the owner's control. No DB migration required.
