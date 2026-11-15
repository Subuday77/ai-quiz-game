# Optional Buy Me a Coffee support CTA

The start screen has a button **About the game** inline with the
**Ready to play?** title, and a separate coffee support row under the intro.

The owner registered the public support page at
`https://buymeacoffee.com/subuday`, now the default donation destination. The app does **not** process payments, accept payment
information, or grant stars for donations.

## Configuring or disabling the CTA

By default the backend returns the owner's URL even when the optional
variable has not been configured on an existing VPS. To override it, add
a variable in the Quiz backend's private `.env`:

```dotenv
BUY_ME_A_COFFEE_URL=https://buymeacoffee.com/subuday
```

Set `BUY_ME_A_COFFEE_URL=` (empty) to deliberately hide the enabled link.
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

Check that the Buy Me a Coffee page and payouts are ready before public launch. No DB migration required.
