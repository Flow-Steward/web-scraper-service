# Browser resource economy

Reduce proxy traffic from optional third-party chat and marketing widgets without
turning a catalogue crawl into a same-origin-only browser. Crawl destination scope
and browser subresource loading remain separate policies.

## Approach

A strict external-host ban breaks product APIs, CDNs and challenge resources.
A new filtering UI adds configuration before there is evidence it is needed.
Extend the existing `block_heavy_resources` mode with a small, explicit list of
known widget/tracking host families instead. This is an implementation choice
under the user's standing instruction to continue checks without blocking on
routine questions. There are no supplier-specific host names in the policy.

## Behavior

- Apply one shared policy to setup browser sessions and crawl browser hooks.
- Keep current image/media/font suppression when the flag is enabled.
- Suppress known third-party widget/tracker requests, including iframe documents.
- Always leave the main document to the existing destination and private-address
  guards. Known tracking hosts used as the actual target remain usable.
- Match parsed host names on DNS label boundaries, ignoring case and a trailing
  DNS dot. Query strings, userinfo and similar names cannot cause false matches.
- Leave unlisted external scripts, product APIs, CDNs, styles and Cloudflare
  resources available. No script rewriting or challenge transport changes.
- `block_heavy_resources=false` disables this economy policy in both consumers.
- Keep the API shape and extension settings unchanged; document the extended flag.

## Validation and limits

Use unit cases for host boundaries and opt-out, and local HTTP proxy/browser
fixtures for both engines. Count actual upstream hits: suppressed resources must
have none while required external scripts still populate product data. Exercise
the job path too. Run the canonical Linux unit and integration suites in CI.
No supplier requests, paid proxy usage, deployment, or old-run resumption is part
of this change. Historic daily CSV totals do not establish future savings.
