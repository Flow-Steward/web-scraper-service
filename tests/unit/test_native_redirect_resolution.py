"""Native browser redirect resolution must not admit parser differentials."""
import pytest


@pytest.mark.parametrize('location',[
    'http://127.0.0.1:8000\\@public.test/probe',
    '//127.0.0.1:8000\\@public.test/probe',
    '///127.0.0.1/private','http:////127.0.0.1/private',
    'http:/127.0.0.1/private','https:private.test/path',
    'https://user:secret@public.test/path','//user@public.test/path',
    ' https://public.test/path','https://pub\tlic.test/path',
    'https://public.test/path\x00','https://public.test/path\x7f',
    '\n//127.0.0.1/private','https://public.test/a b',
])
def test_ambiguous_native_redirect_spellings_are_refused(location):
    from app.fetching.native_redirects import native_redirect_target
    assert native_redirect_target('https://public.test/base/start',location) is None


@pytest.mark.parametrize(('location','expected'),[
    ('../next?p=1','https://public.test/next?p=1'),
    ('/next?q=opaque%20value','https://public.test/next?q=opaque%20value'),
    ('//cdn.test/asset','https://cdn.test/asset'),
    ('https://public.test/path//next','https://public.test/path//next'),
])
def test_proper_native_redirects_preserve_url_identity(location,expected):
    from app.fetching.native_redirects import native_redirect_target
    assert native_redirect_target('https://public.test/base/start',location)==expected
