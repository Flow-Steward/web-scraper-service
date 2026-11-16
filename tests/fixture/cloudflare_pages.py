"""Pages shaped like the real Cloudflare ones (structure and markers as Cloudflare serves them;
tokens and ids are made up). Used by the fixture server and the detection unit tests.

Must NOT be a challenge or block:
* a shop page carrying the JavaScript-detections script (``/cdn-cgi/challenge-platform/scripts/jsd/main.js``);
* a page with a contact-form Turnstile (widget + ``challenges.cloudflare.com`` frame);
* a page with a reCAPTCHA ``data-sitekey`` newsletter form;
* a product whose text says "Access denied".

Must be a challenge / block:
* the managed-challenge interstitial ("Just a moment...", ``_cf_chl_opt``, orchestrate script), HTTP 403;
* the same interstitial as rendered in a browser (Turnstile frame inside, no status);
* the 1020 block page ("Attention Required! | Cloudflare", "Sorry, you have been blocked"), HTTP 403.
"""

JSD_SNIPPET = (
    "<script>(function(){function c(){var b=a.contentDocument||a.contentWindow.document;if(b){var d=b.createElement('script');"
    "d.innerHTML=\"window.__CF$cv$params={r:'8f1d2c3b4a5e6f70',t:'MTczMTQ5NjAwMC4wMDAwMDA='};var a=document.createElement('script');"
    "a.nonce='';a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';document.getElementsByTagName('head')[0].appendChild(a);\";"
    "b.getElementsByTagName('head')[0].appendChild(d)}}if(document.body){var a=document.createElement('iframe');a.height=1;a.width=1;"
    "a.style.position='absolute';a.style.top=0;a.style.left=0;a.style.border='none';a.style.visibility='hidden';"
    "document.body.appendChild(a);if('loading'!==document.readyState)c();else if(window.addEventListener)"
    "document.addEventListener('DOMContentLoaded',c);}})();</script>"
)

CONTACT_TURNSTILE = (
    "<form class='wpcf7-form' action='/kontakt' method='post'><input name='your-email' type='email'>"
    "<div class='cf-turnstile' data-sitekey='0x4AAAAAAAFixtureSiteKey' data-theme='light'>"
    "<iframe src='https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/rcv0/0/abcde/0x4AAAAAAAFixtureSiteKey/light/normal'"
    " title='Widget containing a Cloudflare security challenge' width='300' height='65'></iframe></div>"
    "<script src='https://challenges.cloudflare.com/turnstile/v0/api.js' async defer></script>"
    "<button type='submit'>Senden</button></form>"
)

RECAPTCHA_FORM = (
    "<form class='newsletter' action='/newsletter'><input name='email'>"
    "<div class='g-recaptcha' data-sitekey='6LcFixtureRecaptchaKey000000000000000000'></div>"
    "<script src='https://www.google.com/recaptcha/api.js' async defer></script></form>"
)

ACCESS_DENIED_TEXT = (
    "<p>Hinweis: Ohne passenden Adapter gilt für den Schaltkreis <b>Access denied</b> — das Modul meldet "
    "'request blocked', bis der Treiber gekoppelt ist.</p>"
)


def decorate_normal_page(html: str) -> str:
    """A normal page as served by a Cloudflare zone with JS detections, a Turnstile contact form,
    a reCAPTCHA newsletter box and "access denied" in the text."""
    extra = ACCESS_DENIED_TEXT + CONTACT_TURNSTILE + RECAPTCHA_FORM
    html = html.replace("<div class='product-description'>", "<div class='product-description'>" + ACCESS_DENIED_TEXT, 1)
    return html.replace("</body>", extra + JSD_SNIPPET + "</body>", 1)


def managed_challenge_page(host: str = "shop.example") -> str:
    return (
        "<!DOCTYPE html><html lang=\"en-US\"><head><title>Just a moment...</title>"
        "<meta http-equiv=\"Content-Type\" content=\"text/html; charset=UTF-8\"><meta name=\"robots\" content=\"noindex,nofollow\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><style>*{box-sizing:border-box;margin:0;padding:0}</style>"
        "</head><body class=\"no-js\"><div class=\"main-wrapper\" role=\"main\"><div class=\"main-content\">"
        f"<noscript><div class=\"h2\"><span id=\"challenge-error-text\">Enable JavaScript and cookies to continue</span></div></noscript>"
        "</div></div><script>(function(){window._cf_chl_opt={cvId: '3',cZone: '" + host + "',cType: 'managed',"
        "cRay: '8f1d2c3b4a5e6f70',cH: 'fixtureHash',cUPMDTk: \"/?__cf_chl_tk=fixtureToken\",cFPWv: 'b',cITimeS: '1731496000',"
        "cTplC:0,cTplV:5,cTplB: 'cf',fa:\"/?__cf_chl_f_tk=fixtureToken\",md: 'fixture',mdrd: 'fixture'};"
        "var cpo = document.createElement('script');cpo.src = '/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?ray=8f1d2c3b4a5e6f70';"
        "window._cf_chl_opt.cOgUHash = location.hash === '' && location.href.indexOf('#') !== -1 ? '#' : location.hash;"
        "document.getElementsByTagName('head')[0].appendChild(cpo);}());</script></body></html>"
    )


def managed_challenge_rendered(host: str = "shop.example") -> str:
    """The interstitial after its script ran in a browser: text and the Turnstile frame."""
    return managed_challenge_page(host).replace(
        "<div class=\"main-content\">",
        "<div class=\"main-content\"><h1 class=\"zone-name-title h1\">" + host + "</h1>"
        "<h2 class=\"h2\" id=\"challenge-running\">Verifying you are human. This may take a few seconds.</h2>"
        "<div id=\"turnstile-wrapper\" class=\"h2\"><div><iframe src=\"https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/rcv0/0/fx/0x4AAAAAAADnPIDROrmt1Wwj/auto/fbE/new/normal/auto/\""
        " title=\"Widget containing a Cloudflare security challenge\" width=\"300\" height=\"65\"></iframe></div></div>"
        "<div class=\"core-msg spacer\">" + host + " needs to review the security of your connection before proceeding.</div>",
    )


def block_1020_page(host: str = "shop.example") -> str:
    return (
        "<!DOCTYPE html><html class=\"no-js\" lang=\"en-US\"><head><title>Attention Required! | Cloudflare</title>"
        "<meta charset=\"UTF-8\" /></head><body><div id=\"cf-wrapper\"><div id=\"cf-error-details\" class=\"cf-error-details-wrapper\">"
        "<div class=\"cf-wrapper cf-header cf-error-overview\"><h1 data-translate=\"block_headline\">Sorry, you have been blocked</h1>"
        f"<h2 class=\"cf-subheadline\"><span data-translate=\"unable_to_access\">You are unable to access</span> {host}</h2></div>"
        "<div class=\"cf-section cf-wrapper\"><h2 data-translate=\"blocked_why_headline\">Why have I been blocked?</h2>"
        "<p>This website is using a security service to protect itself from online attacks.</p></div>"
        "<div class=\"cf-error-footer\"><p>Cloudflare Ray ID: <strong>8f1d2c3b4a5e6f70</strong> &bull; Error 1020</p></div>"
        "</div></div></body></html>"
    )
