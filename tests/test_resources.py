import pytest

from compdesign_bot.models import ArticleLink
from compdesign_bot.resources import public_resource_url, source_links


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,test",
        "file:///etc/passwd",
        "https://user:password@example.com/demo",
        "https://@example.com/demo",
        "https://example.com@localhost/demo",
        "https://localhost/demo",
        "https://local.test/demo",
        "http://build.internal/demo",
        "http://127.0.0.1/demo",
        "https://8.8.8.8/demo",
        "http://[::1]/demo",
        "http://[2606:4700:4700::1111]/demo",
        "http://2130706433/demo",
        "http://0x7f.0.0.1/demo",
        "https://example.com:8000/demo",
        "https://example.com:invalid/demo",
        "https://example.com/\nredirect",
        "https://exam ple.com/demo",
        "https://example.com/a b",
        "https://example.com/a%0d%0ab",
        "https://example.com%2f.evil.org/demo",
        "https://example.com\\@evil.org/demo",
        "https://example.com/<script>",
        "https://example.com/\"onclick=alert(1)",
        "https://example.com/" + "x" * 2048,
    ],
)
def test_rejects_unsafe_or_nonpublic_urls(url):
    assert public_resource_url(url) == ""


def test_normalizes_public_links_without_changing_query_identity():
    assert public_resource_url("https://EXAMPLE.com:443/demo?utm_source=rss&amp;scene=2#top") == (
        "https://example.com/demo?scene=2"
    )
    assert public_resource_url("/project", "https://example.com/story") == "https://example.com/project"
    assert public_resource_url("//other.org/demo", "https://example.com/story") == "https://other.org/demo"
    assert public_resource_url("/project", "http://localhost/story") == ""


def test_retains_only_explicit_links_and_prioritizes_code_demo():
    raw = """<p>A paper about computational design.</p>
    <a href="https://example.com/story">Project page</a>
    <a href="https://project.org/">Project page</a>
    <a href="https://example.org/docs">Documentation</a>
    <a href="https://demo.org/play">Live demo</a>
    <a href="https://github.com/compas-dev/compas?utm_source=rss">Source code</a>
    <a href="https://github.com/compas-dev/compas">GitHub again</a>
    <a href="https://unrelated.org/">Sponsor</a>
    """
    assert source_links(raw, "https://example.com/story") == (
        ArticleLink("코드 (원문 링크)", "https://github.com/compas-dev/compas"),
        ArticleLink("데모 (원문 링크)", "https://demo.org/play"),
        ArticleLink("프로젝트 (원문 링크)", "https://project.org/"),
    )


def test_extracts_plain_abstract_project_url_and_repository_without_guesses():
    raw = """We introduce a new geometry method. Code: https://github.com/lab/geometry.
    Project page: https://geometry-lab.org/method/.
    Read our announcement at https://unrelated.org/news.
    """
    assert source_links(raw, "https://arxiv.org/abs/2609.12345") == (
        ArticleLink("코드 (원문 링크)", "https://github.com/lab/geometry"),
        ArticleLink("프로젝트 (원문 링크)", "https://geometry-lab.org/method/"),
    )
    assert source_links("Code for AmazingPaper will be available soon.", "https://arxiv.org/abs/123") == ()


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/author",
        "https://github.com/author/repo/issues/12",
        "https://github.com/author/repo/pull/12",
        "https://github.com/author/repo/actions",
        "https://github.com/author/repo/releases/tag/v1",
        "https://github.com/topics/generative-art",
        "https://github.com/features/actions",
        "https://github.com/orgs/example",
        "https://github.com/author/repo/tree",
        "https://github.com/author/repo/tree/main/../../issues",
        "https://github.com/author/repo/tree/main/%2e%2e/issues",
        "https://github.com.evil.org/author/repo",
        "https://evil-github.com/author/repo",
    ],
)
def test_github_navigation_and_lookalikes_are_not_presented_as_code(url):
    assert source_links(f'<a href="{url}">Project code demo</a>', "https://example.org/story") == ()


@pytest.mark.parametrize("path", ["", "/", "/tree/main/examples", "/blob/main/README.md"])
def test_repository_and_source_file_links_are_retained(path):
    url = "https://github.com/author/repo" + path
    assert source_links(f'<a href="{url}">GitHub</a>', "https://example.org/story") == (
        ArticleLink("코드 (원문 링크)", url),
    )


def test_relative_demo_and_docs_links_have_neutral_labels():
    assert source_links(
        '<a href="../demo">데모 체험</a><a href="/manual">사용 문서</a>',
        "https://example.org/posts/story",
    ) == (
        ArticleLink("데모 (원문 링크)", "https://example.org/demo"),
        ArticleLink("문서 (원문 링크)", "https://example.org/manual"),
    )


def test_hidden_markup_and_unsafe_anchors_are_ignored():
    raw = """<script>https://github.com/hidden/script</script>
    <style>https://github.com/hidden/style</style>
    <noscript><a href="https://github.com/hidden/noscript">Code</a></noscript>
    <a href="javascript:alert(1)">Demo</a>
    <a href="https://user:secret@project.org/">Project page</a>
    <a href="https://github.com/lab/visible">&lt;img src=x onerror=alert(1)&gt;</a>
    """
    assert source_links(raw, "https://example.org/story") == (
        ArticleLink("코드 (원문 링크)", "https://github.com/lab/visible"),
    )


def test_anchor_labels_are_not_copied_into_output():
    links = source_links('<a href="https://demo.org/">Demo &lt;b&gt;BUY NOW&lt;/b&gt;</a>', "https://example.org/")
    assert links == (ArticleLink("데모 (원문 링크)", "https://demo.org/"),)


def test_generic_project_mentions_do_not_establish_project_page_links():
    assert source_links(
        '<a href="https://example.org/sponsor">Sponsor this project</a>',
        "https://example.org/story",
    ) == ()


def test_source_and_candidate_counts_are_bounded():
    raw = " ".join(f'<a href="https://github.com/lab/repo{index}">Code</a>' for index in range(100))
    assert len(source_links(raw, "https://example.org/story")) == 3
    assert source_links("x" * 60_000 + raw, "https://example.org/story") == ()
