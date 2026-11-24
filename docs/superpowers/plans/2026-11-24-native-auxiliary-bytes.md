# Preserve native auxiliary response bytes

Implement inline with independent review before publication.

Partecha's bounded setup returned zero sitemap URLs after a successful browser clearance. A separate transport probe can pass, so this is not a permanent site denial. The current setup and runtime both discard the native document and read it again through APIRequestContext. A deterministic local server that permits the cleared native browser but gates the context client reproduces a 403 after successful clearance.

- [x] Reproduce with a real browser and local server, without proxy: expected XML, got context-client 403.
- [x] Retain only the latest native main-document GET response for each bound page navigation, never frame/asset/POST bodies or previous navigations.
- [x] Reuse native XML bytes after clearance in setup and runtime, preserving destination policy and bounded body/error handling.
- [x] Test both browser engines, rebind/lifecycle, denial and body-read failure; independent review found no remaining important findings. Native regression cases passed (19); final lifecycle/status boundaries and test_wait_step passed (14).
- [ ] Finish the complete fixture suite (currently running).
- [ ] Verify bounded Partecha collection before launching fresh full runs. Old held runs remain paused.

Independent review found two lifecycle/read-duration defects. Six regressions reproduced them before adding request-start ownership, binding epochs, before/after read validation and a finite native-body timeout. Native document commit correlation also reproduced and fixes stale initial403 classification after a successful auto-clear reload; a real403 remains denied. The protection fixture now accepts zero budget consumption if clearance completed before detection, while requiring every requested record.
