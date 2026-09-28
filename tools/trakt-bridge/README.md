# Infuse → Nuvio Sync bridge

Runs on the home Raspberry Pi. Control D points `api.trakt.tv` and `apiz.trakt.tv` at the Pi; the
Pi presents a certificate the devices trust, passes every request through to the real Trakt, and
turns Infuse's scrobble start/stop reports into exact playback positions (and watched marks at
90%+) on the "Saad & Sabrina" Nuvio Sync profile.

Settings (in `~/trakt-bridge/.env` on the Pi): `NUVIO_EMAIL`, `NUVIO_PASSWORD`, `TMDB_API_KEY`,
`NUVIO_PROFILE_NAME`.
