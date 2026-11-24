# Fork patches (iPhone, on top of Nuvio Enhanced)

Patches are grouped **by file**, not by feature: every upstream file the fork edits lives in
exactly one patch, so an Enhanced update that touches a file causes at most one conflict, and
the patches apply in any order. `BASE` is the Enhanced commit they were made against.

| Patch | Area | Features in it |
|---|---|---|
| `01-infuse-playback-and-subtitles` | player/, streams, playback settings | Infuse playback (first-play duration, redirected streams, release filename, profile stamp for the Trakt bridge), subtitles to Infuse (forwarding, Kuiper relay, sync to the video, no add-on subtitles option, open without waiting for sync), Up Next in Infuse with year and subtitle names, "Are you still watching?", More Like This after a movie, warm streams and hide empty sources, Hindi / Multi-audio tags on sources and sources in the profile's audio language listed first |
| `02-home-screen-and-rows` | home/, notifications | Top 10 numbers, Netflix Top 10 and Trending rows (no Trending Now), Because You Watched (+ toggle), Remind Me, find sources ahead from Continue Watching, kids-friendly Top 10, kids rating limit (Home), Netflix Home (genre line, "Continue Watching for", Netflix cards), Coming Soon row, "What's new in your fork" note, hide cinema-only films, Play Something (shuffle) button, language tag (KOREAN, HINDI…) on non-English titles, Remind Me notifications as soon as a reminder syncs |
| `03-home-components` | home/components/ | Remaining episodes on Continue Watching, Netflix Top 10 / catalog / collection row looks, still covers for collection GIFs, card tags (TOP 10 / New Episode / Coming Soon), Netflix poster cards and hero section |
| `04-theme-style-and-home-settings` | core/ui/, MainActivity, HomescreenSettingsPage | Style per profile: Netflix / Disney+ / Max / Custom (`ForkStyle.kt`, recolours the whole app through `core/ui/Theme.kt`), Disney+ Style for Kids, Home Extras settings, `ShelfComponents.kt` and `NuvioAsyncImage.ios.kt` |
| `05-search-and-collections` | search/, collection/ | Kids rating limit and fork looks in Search and Collection folder browsing |
| `06-detail-page` | details/, detail page settings | Netflix-style details page and White Play button, find sources ahead on the detail page, Top 10 badge, kids filter on More Like This, original language ("Korean") in the info line, backdrop kept until the trailer actually plays |
| `07-tmdb-lookups` | tmdb/TmdbMetadataService | Trending / kids popular lists, Coming Soon (streaming dates), recommendations, age ratings, card freshness and original language, studio logos, studio pages without TMDB enrichment, cinema-only release checks |
| `08-tracking-and-watched-fixes` | watched/, watching/, watchprogress/ | TMDB catalog watched badge, large-series watched freeze fix |
| `09-profiles` | profiles/ | "Who's watching" polish (greeting, Kids tag, last-used ring) |
| `10-app-shell-and-settings` | MainAppContent, AppShellComponents, Advanced settings | Wiring for the above (Infuse launch, Remind Me, studio tiles on the Home tab), clear image cache |

To change a feature, edit the patched source and regenerate the patch for that file's group
(`git diff BASE HEAD -- <files in the group>`).
