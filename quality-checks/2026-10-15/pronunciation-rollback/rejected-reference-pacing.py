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


def phrase_chunks(text: str, max_chars: int = 200) -> list[str]:
    """Keep complete sentences where possible, with bounded synthesis context."""
    sentences = re.split(r'(?<=[.!?।॥])\s+|\n+', text.strip())
    phrases: list[str] = []
    for sentence in sentences:
        tags = re.match(r'^((?:<[^<>]+>\s*)+)(.*)$', sentence)
        if tags and phrases:
            phrases[-1] += ' ' + tags.group(1).strip()
            sentence = tags.group(2)
        current = ''
        for word in sentence.split():
            candidate = f'{current} {word}'.strip()
            if current and (len(candidate) > max_chars or speech_units(candidate) > 60):
                phrases.append(current)
                current = word
            else:
                current = candidate
        if current:
            if phrases and speech_units(phrases[-1]) < 12 and len(phrases[-1]) + len(current) < max_chars:
                phrases[-1] += ' ' + current
            else:
                phrases.append(current)
    merged: list[str] = []
    for phrase in phrases:
        joined = f'{merged[-1]} {phrase}' if merged else phrase
        if merged and len(joined) <= max_chars and speech_units(joined) <= 60:
            merged[-1] = joined
        else:
            merged.append(phrase)
    return merged


def continuity_chunks(text: str, max_chars: int = 200) -> list[str]:
    """Start long requests with a short, transcribable voice reference.

    Prefer the first complete sentence. Bound longer openings so their
    acoustic reference leaves enough model context for subsequent phrases.
    """
    phrases = phrase_chunks(text, max_chars)
    if len(phrases) < 2:
        return phrases
    words = phrases[0].split()
    opener = []
    for word in words:
        candidate = ' '.join([*opener, word])
        if opener and (len(candidate) > min(90, max_chars) or speech_units(candidate) > 24):
            break
        opener.append(word)
        if word.endswith(('.', '!', '?', '।', '॥')) and speech_units(candidate) >= 8:
            break
    opening = ' '.join(opener)
    remaining = ' '.join([*words[len(opener):], *phrases[1:]])
    return [opening, *phrase_chunks(remaining, max_chars)]


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
