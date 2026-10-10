# Infuse → Nuvio Sync bridge

Runs on the home Raspberry Pi. Control D points `api.trakt.tv` and `apiz.trakt.tv` at the Pi; the
Pi presents a certificate the devices trust, passes every request through to the real Trakt, and
turns Infuse's scrobble start/stop reports into exact playback positions (and watched marks at
90%+) on the "Saad & Sabrina" Nuvio Sync profile.

Settings (in `~/trakt-bridge/.env` on the Pi): `NUVIO_EMAIL`, `NUVIO_PASSWORD`, `TMDB_API_KEY`,
`NUVIO_PROFILE_NAME`.

Optional: set `SCROB_URL` (e.g. `https://scrob.khanofmilton.ca`) and `SCROB_API_KEY` (Scrob →
Connections → API Key) to also send each start/pause/stop to Scrob's Kodi webhook as it happens,
so Scrob and the Simkl/WeTrakr/MDBList accounts it feeds update at once instead of on its
15-minute Nuvio pull. Only plays saved to `SCROB_PROFILE_NAME` (default: the default profile
above) are sent, so the Kids profile stays out of Scrob.
