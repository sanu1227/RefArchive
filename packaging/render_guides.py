"""Generate offline HTML manuals from the maintained developer documentation."""
from html import escape
from pathlib import Path
from markdown import Markdown

ROOT = Path(__file__).resolve().parents[1]


def render_guides():
    for source, name, title in [('user-guide.md', 'user-guide.html', '사용설명서'), ('distribution.md', 'distribution-guide.html', '배포 안내')]:
        text = (ROOT / 'docs' / source).read_text(encoding='utf-8')
        if source == 'distribution.md':
            # Recipients need installation and transfer instructions, not build commands.
            text = text.split('\n## 다시 빌드하기', 1)[0].replace('`release/', '`')
        markdown = Markdown(extensions=['toc', 'fenced_code', 'tables', 'sane_lists'], extension_configs={'toc': {'toc_depth': '2-3'}})
        body = markdown.convert(text)
        html = f'''<!doctype html>
<html lang="ko">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>RefArchive · {escape(title)}</title><link rel="stylesheet" href="guide.css"></head>
<body><a class="skip-link" href="#content">본문 바로가기</a>
<header class="guide-header"><div><span class="brand">RefArchive</span><p>이미지·PDF·링크를 모으고 다시 찾는 나의 자료함</p></div>
<nav aria-label="안내 종류"><a href="user-guide.html" {'aria-current="page"' if source == 'user-guide.md' else ''}>사용설명서</a><a href="distribution-guide.html" {'aria-current="page"' if source == 'distribution.md' else ''}>설치 · 배포 안내</a></nav></header>
<div class="guide-layout"><aside class="guide-toc"><details open><summary>목차 · 원하는 항목으로 이동</summary>{markdown.toc}</details></aside>
<main id="content" tabindex="-1"><div class="quick-start"><strong>처음 사용하시나요?</strong><p>RefArchive를 실행하고 <b>+ 자료 추가</b>로 자료를 등록하세요. 종료는 <b>보관 · 백업 → 프로그램 종료</b>에서 합니다.</p></div>
<article>{body}</article><footer>이 안내는 인터넷이나 프로그램 실행 없이도 읽을 수 있습니다. 파일을 열면 기본 브라우저에서 표시됩니다.</footer></main></div></body></html>
'''
        (ROOT / 'docs' / name).write_text(html, encoding='utf-8')


if __name__ == '__main__':
    render_guides()
