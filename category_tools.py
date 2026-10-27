"""Conservative, local category matching. Scores are evidence strength, not probabilities."""
from collections import Counter
from functools import lru_cache
import math
import re
import unicodedata

DEFAULT = {'mode': 'auto', 'description': '', 'include': [], 'exclude': []}
AUTO_THRESHOLD = .85
MARGIN = .15


def normalize(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


def clean_config(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT) or value['mode'] not in ('off', 'suggest', 'auto'):
        raise ValueError('카테고리 분류 설정을 확인하세요.')
    description = value['description']
    if not isinstance(description, str) or len(description) > 1000 or '\x00' in description:
        raise ValueError('카테고리 설명은 최대 1,000자입니다.')
    result = {'mode': value['mode'], 'description': description.strip()}
    for field in ('include', 'exclude'):
        terms = value[field]
        if not isinstance(terms, list) or len(terms) > 30 or any(not isinstance(term, str) or not term.strip() or len(term) > 100 or '\x00' in term for term in terms):
            raise ValueError('포함·제외 문구는 각각 최대 30개, 문구당 1~100자입니다.')
        result[field] = list(dict.fromkeys(term.strip() for term in terms))
    if {normalize(term) for term in result['include']} & {normalize(term) for term in result['exclude']}:
        raise ValueError('같은 문구를 포함과 제외에 동시에 넣을 수 없습니다.')
    return result


def sources(item, bundles):
    values = [(field, 0, item[field]) for field in ('title', 'filename', 'note')]
    for bundle in bundles:
        if item['id'] in bundle['ids']:
            values += [(field, 0, bundle[field]) for field in ('title', 'note')]
    values += [('content', page['number'], page['text']) for page in (item.get('analysis') or {}).get('pages', [])]
    return [(field, page, normalize(text)) for field, page, text in values if text.strip()]


def contains(text, term):
    term = normalize(term)
    # English word boundaries avoid matching "art" inside "party"; Korean suffixes remain searchable.
    if re.fullmatch('[a-z0-9 ]+', term):
        return re.search(r'(?<![a-z0-9])' + re.escape(term) + r'(?![a-z0-9])', text) is not None
    return term in text


@lru_cache(maxsize=2048)
def features(text):
    counts = Counter()
    for word in re.findall(r'[^\W\d_]{2,}', normalize(text)):
        counts['w:' + word] += 2
        for size in (2, 3):
            for index in range(len(word) - size + 1):
                counts[word[index:index + size]] += 1
    # Limit repeated boilerplate and the size of comparisons, not the original archived text.
    values = {key: 1 + math.log(min(count, 3)) for key, count in counts.most_common(1000)}
    norm = math.sqrt(sum(value * value for value in values.values())) or 1
    return {key: value / norm for key, value in values.items()}


def similarity(left, right):
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(key, 0) for key, value in left.items())


def sample_text(item, bundles):
    parts = sources(item, bundles)
    return '\n'.join(text[:4000] for _, _, text in parts[:6])[:16000]


def predict(snapshot, target):
    parts = sources(target, snapshot['bundles'])
    vector = features(sample_text(target, snapshot['bundles']))
    owners = {key: bundle['id'] for bundle in snapshot['bundles'] for key in bundle['ids']}
    target_owner = owners.get(target['id'], target['id'])
    previous = {row['name']: row for row in snapshot['categoryPredictions'] if row['item'] == target['id']}
    candidates = []
    for rule in snapshot['categoryRules']:
        name = rule['name']
        if rule['mode'] == 'off' or previous.get(name, {}).get('status') == 'rejected':
            continue
        if any(contains(text, term) for term in rule['exclude'] for _, _, text in parts):
            continue
        score, reasons, references = 0, [], []
        for term in dict.fromkeys([name, *rule['include']]):
            hits = [(field, page) for field, page, text in parts if contains(text, term)]
            if not hits:
                continue
            field, page = min(hits, key=lambda hit: ('title', 'filename', 'note', 'content').index(hit[0]))
            strength = {'title': .92, 'filename': .90, 'note': .62, 'content': .58}[field]
            if len(normalize(term)) < 2:
                strength = min(strength, .4)
            score = max(score, strength)
            reasons.append({'term': term, 'field': field, 'page': page})
        # Multiple independent, explicit positive phrases may support a body-only document.
        explicit = []
        for term in sorted({normalize(match['term']) for match in reasons if match['term'] in rule['include'] and len(normalize(match['term'])) >= 2}, key=len, reverse=True):
            if not any(term in other for other in explicit):
                explicit.append(term)
        if len(explicit) >= 2:
            score = max(score, .90)
        if rule['description']:
            overlap = similarity(vector, features(rule['description']))
            if overlap >= .25:
                score = max(score, min(.70, overlap))
                reasons.append({'term': '카테고리 설명과 표현 유사', 'field': 'description', 'page': 0})
        seen, examples = set(), []
        # ponytail: compare at most 20 recent confirmed entries per category; index them for larger libraries.
        for item in snapshot['items']:
            owner = owners.get(item['id'], item['id'])
            if owner == target_owner or owner in seen or item['trashed'] or name not in item['categories'] or name in item['autoCategories']:
                continue
            seen.add(owner)
            other = features(sample_text(item, snapshot['bundles']))
            if len(vector) >= 15 and len(other) >= 15:
                examples.append((similarity(vector, other), item['id']))
            if len(seen) >= 20:
                break
        examples.sort(reverse=True)
        if examples:
            support = sum(value for value, _ in examples[:2]) / min(2, len(examples))
            # A single near duplicate can suggest; automatic reuse needs 3 independent entries and 2 close matches.
            evidence_score = .90 if len(examples) >= 3 and support >= .82 and examples[1][0] >= .78 else min(.72, support)
            if evidence_score >= .30:
                score = max(score, evidence_score)
                references = [key for _, key in examples[:3]]
                reasons.append({'term': f'확정 사례 표현 유사도 {support:.2f}', 'field': 'example', 'page': 0})
        if score >= .30:
            candidates.append({'item': target['id'], 'name': name, 'score': round(score, 4),
                'reasons': reasons[:31], 'evidence': references, 'mode': rule['mode']})
    candidates.sort(key=lambda row: (-row['score'], row['name']))
    manual = set(target['categories']) - set(target['autoCategories'])
    for index, candidate in enumerate(candidates[:3]):
        gap = candidate['score'] - (candidates[1]['score'] if len(candidates) > 1 else 0)
        automatic = index == 0 and candidate['mode'] == 'auto' and not manual and candidate['score'] >= AUTO_THRESHOLD and gap + 1e-9 >= MARGIN
        candidate['status'] = 'applied' if automatic else 'pending'
        candidate['reason'] = ('근거 충분·다른 후보와 차이 있음' if automatic else
            '직접 지정한 카테고리 우선' if manual else '추천만 설정' if candidate['mode'] == 'suggest' else
            '다른 후보와 경합' if candidate['score'] >= AUTO_THRESHOLD else '자동 배정 기준 미달')
        candidate.pop('mode')
    return candidates[:3]
