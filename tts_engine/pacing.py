"""Phrase preparation and pitch-preserving speech pace control."""

from __future__ import annotations

import asyncio
import re
import unicodedata

SAMPLE_RATE = 24000
_TAGS = re.compile(r'<[^<>]*>')
_VOWELS = set('A AA I II U UU VOCALIC R VOCALIC RR VOCALIC L VOCALIC LL E EE AI O OO AU AE OE'.split())


def speech_units(text: str) -> int:
    """Estimate spoken syllables across the Latin and Indic preset scripts.

    Indic vowel signs belong to their consonant, and viramas suppress its
    inherent vowel. This avoids counting Unicode combining marks as speech.
    The estimate is a pacing guide rather than a pronunciation dictionary.
    """
    total = 0
    for word in re.findall(r'\w[\w\u0900-\u0dff]*', _TAGS.sub('', text), re.UNICODE):
        if word.isascii():
            if word.isdigit():
                total += max(1, len(word) * 2)
                continue
            groups = len(re.findall(r'[aeiouy]+', word.lower()))
            if word.lower().endswith('e') and not word.lower().endswith(('le', 'ye')) and groups > 1:
                groups -= 1
            total += max(1, groups)
            continue
        count, last_consonant, script = 0, False, ''
        for char in word:
            name = unicodedata.name(char, '')
            if 'LETTER ' in name:
                script = name.split(' LETTER ')[0]
                letter = name.split(' LETTER ')[-1]
                count += 1
                last_consonant = letter not in _VOWELS and not letter.startswith('VOCALIC')
            elif 'VIRAMA' in name or 'HALANT' in name:
                count = max(0, count - 1)
                last_consonant = False
            elif 'VOWEL SIGN' in name:
                last_consonant = False
        # Common final schwa suppression in the northern preset languages.
        if last_consonant and count > 1 and script in ('DEVANAGARI', 'GUJARATI', 'GURMUKHI', 'BENGALI'):
            count -= 1
        total += max(1, count)
    return total


_CLAUSE_END = tuple(',;:\u060c\u061b\u2014\u2013\u2026')


def _split_sentence(sentence: str, max_chars: int, max_units: int) -> list[str]:
    """Cut one over-long sentence into balanced pieces at natural pauses.

    Every cut starts a new, independently sampled generation, and the model
    re-draws the voice each time. Cuts are therefore as few as the limits
    allow, they fall after a comma or similar pause where a speaker would
    pause anyway, and the pieces are even so none is a tiny leftover that
    would be spoken in an unstable voice.
    """
    words = sentence.split()
    count = len(words)
    units = [speech_units(word) for word in words]
    total_units = sum(units)
    total_chars = len(sentence)
    if count < 2 or (total_chars <= max_chars and total_units <= max_units):
        return [sentence]
    prefix_units = [0]
    prefix_chars = [-1]
    for word, unit in zip(words, units):
        prefix_units.append(prefix_units[-1] + unit)
        prefix_chars.append(prefix_chars[-1] + len(word) + 1)

    def fits(i: int, j: int) -> bool:
        return (prefix_units[j] - prefix_units[i] <= max_units and
                prefix_chars[j] - prefix_chars[i] <= max_chars)

    minimum = max(-(-total_units // max_units), -(-total_chars // max_chars), 2)
    best: tuple[float, list[int]] | None = None
    for pieces in (minimum, minimum + 1):
        ideal = total_units / pieces
        inf = float('inf')
        cost = [[inf] * (count + 1) for _ in range(pieces + 1)]
        back = [[0] * (count + 1) for _ in range(pieces + 1)]
        cost[0][0] = 0.
        for piece in range(1, pieces + 1):
            for j in range(piece, count + 1):
                for i in range(piece - 1, j):
                    if cost[piece - 1][i] == inf or not fits(i, j):
                        continue
                    size = (prefix_units[j] - prefix_units[i] - ideal) / ideal
                    # A cut that is not after a pause costs about as much as
                    # a piece that is half again too long or too short.
                    cut = 0. if j == count or words[j - 1].endswith(_CLAUSE_END) else .3
                    value = cost[piece - 1][i] + size * size + cut
                    if value < cost[piece][j]:
                        cost[piece][j], back[piece][j] = value, i
        if cost[pieces][count] < inf and (best is None or cost[pieces][count] < best[0]):
            cuts, j = [], count
            for piece in range(pieces, 0, -1):
                cuts.append(j)
                j = back[piece][j]
            best = (cost[pieces][count], sorted(cuts))
    if best is None:  # a single word is over the limit; fall back to greedy
        parts, current = [], ''
        for word in words:
            candidate = f'{current} {word}'.strip()
            if current and (len(candidate) > max_chars or speech_units(candidate) > max_units):
                parts.append(current)
                current = word
            else:
                current = candidate
        return parts + ([current] if current else [])
    parts, start = [], 0
    for end in best[1]:
        parts.append(' '.join(words[start:end]))
        start = end
    return parts


def phrase_chunks(text: str, max_chars: int = 200, max_units: int = 60,
                  sentence_chars: int | None = None,
                  sentence_units: int | None = None) -> list[str]:
    """Group text into phrases, one model generation each.

    The model draws a slightly different voice for every generation, so a
    seam between two phrases can sound like a different speaker. Short
    sentences are grouped up to `max_chars`/`max_units`. A single sentence
    is never cut while it fits `sentence_chars`/`sentence_units` (default:
    the same limits); only longer ones are cut, at pauses, into even pieces.
    """
    sentence_chars = max(sentence_chars or 0, max_chars)
    sentence_units = max(sentence_units or 0, max_units)
    sentences = re.split(r'(?<=[.!?\u0964\u0965])\s+|\n+', text.strip())
    phrases: list[str] = []
    for sentence in sentences:
        tags = re.match(r'^((?:<[^<>]+>\s*)+)(.*)$', sentence)
        if tags and phrases:
            phrases[-1] += ' ' + tags.group(1).strip()
            sentence = tags.group(2)
        pieces = _split_sentence(' '.join(sentence.split()), sentence_chars, sentence_units)
        phrases.extend(piece for piece in pieces if piece)
    merged: list[str] = []
    for index, phrase in enumerate(phrases):
        joined = f'{merged[-1]} {phrase}' if merged else phrase
        if merged and len(joined) <= max_chars and speech_units(joined) <= max_units:
            merged[-1] = joined
        else:
            merged.append(phrase)
    return merged


async def prepare_phrase(pcm: bytes, text: str, speed: float, steady_volume: bool) -> bytes:
    from .loudness import PhraseLoudnessNormalizer

    if not pcm:
        return pcm
    # At normal speed the decoded waveform bypasses time stretching entirely.
    # Text-derived target durations changed cadence and voice texture even at
    # speed=1.0, so speed is now applied only when explicitly requested.
    output = pcm
    if abs(speed - 1.0) > 1e-8:
        tempo = float(speed)
        filters = []
        while tempo > 2:
            filters.append('atempo=2')
            tempo /= 2
        while tempo < .5:
            filters.append('atempo=0.5')
            tempo /= .5
        filters.append(f'atempo={tempo:.8f}')
        process = await asyncio.create_subprocess_exec(
            'ffmpeg', '-nostdin', '-v', 'error', '-f', 's16le', '-ar', str(SAMPLE_RATE),
            '-ac', '1', '-i', 'pipe:0', '-af', ','.join(filters), '-f', 's16le', 'pipe:1',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            output, error = await asyncio.wait_for(process.communicate(pcm), timeout=30)
            if process.returncode or not output:
                raise RuntimeError('Speech tempo processing failed: ' + error.decode(errors='replace')[:200])
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    if steady_volume:
        # Estimate the whole phrase before emitting its first samples.
        output = PhraseLoudnessNormalizer().process(output)
    return output
