# Fork patches (iPhone, on top of Nuvio Enhanced)

Patches are grouped **by file**, not by feature: every upstream file the fork edits lives in
exactly one patch, so an Enhanced update that touches a file causes at most one conflict, and
the patches apply in any order. `BASE` is the Enhanced commit they were made against.

| Patch | Area | Features in it |
|---|---|---|
| `01-infuse-playback-and-subtitles` | player/, streams, playback settings | Infuse playback (first-play duration, redirected streams, release filename, profile stamp for the Trakt bridge), subtitles to Infuse (forwarding, Kuiper relay, sync to the video, no add-on subtitles option, open without waiting for sync), Up Next in Infuse with year and subtitle names, "Are you still watching?", More Like This after a movie, warm streams and hide empty sources, Hindi / Multi-audio tags on sources and sources in the profile's audio language listed first |
| `02-home-rows-and-looks` | home/, search, collections, home settings | Top 10 numbers, Netflix Top 10 and Trending rows (no Trending Now), Because You Watched (+ toggle), Remind Me, remaining episodes on Continue Watching, find sources ahead from Continue Watching, kids-friendly Top 10, kids rating limit (Home, search, folders), Netflix Home (genre line, "Continue Watching for", Netflix cards), card tags (TOP 10 / New Episode / Coming Soon, saved and fetched ahead), Coming Soon row, "What's new in your fork" note, Disney+ Style for Kids, hide cinema-only films, still covers for collection GIFs, Play Something (shuffle) button, language tag (KOREAN, HINDI…) on non-English titles, Remind Me notifications as soon as a reminder syncs |
| `03-detail-page` | details/, detail page settings | Netflix-style details page and White Play button, find sources ahead on the detail page, Top 10 badge, kids filter on More Like This, original language ("Korean") in the info line, backdrop kept until the trailer actually plays |
| `04-tmdb-lookups` | tmdb/TmdbMetadataService | Trending / kids popular lists, Coming Soon (streaming dates), recommendations, age ratings, card freshness and original language, studio logos, studio pages without TMDB enrichment, cinema-only release checks |
| `05-tracking-and-watched-fixes` | simkl/, watched/, watching/, watchprogress/ | Simkl auth v2, TMDB catalog watched badge, large-series watched freeze fix |
| `06-profiles` | profiles/ | "Who's watching" polish (greeting, Kids tag, last-used ring) |
| `07-app-shell-and-settings` | MainAppContent, AppShellComponents, Advanced settings | Wiring for the above (Infuse launch, Remind Me, studio tiles on the Home tab), clear image cache |

To change a feature, edit the patched source and regenerate the patch for that file's group
(`git diff BASE HEAD -- <files in the group>`).
