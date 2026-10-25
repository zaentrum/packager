// Writes src/packager/regions.py: the names a rendition's NAME gives a
// language's script and region, the regions that are a language's own, and
// the names a title may give a language, a region or a regional form of it,
// from CLDR as Node's Intl gives them - the data chino-web names tracks by
// (src/lib/languages.ts: Intl.DisplayNames with languageDisplay 'standard',
// regionCodesOf, languageNames, regionsOf, dialectsOf). Python has no CLDR,
// so the packager keeps this copy.
//
//   node scripts/regions.mjs > src/packager/regions.py
//
// Run it again when the clients' Intl moves to a CLDR that names a region
// otherwise; tests/test_track_names.py holds the names the spec gives.

import { readFileSync } from 'node:fs';

const here = new URL('..', import.meta.url);
const source = readFileSync(new URL('src/packager/packager.py', here), 'utf8');
const table = source.split('_LANGUAGE_TABLE = """\\\n')[1].split('"""')[0];
// Each language by its key: the 639-1 code where it has one, else 639-2/T.
const bases = table
  .trim()
  .split('\n')
  .map((row) => row.split(/\s+/))
  .map(([one, t]) => (one !== '-' ? one : t))
  .filter((code) => !['mul'].includes(code));

const standard = new Intl.DisplayNames(['en'], { type: 'language', languageDisplay: 'standard', fallback: 'none' });
const regionNamed = new Intl.DisplayNames(['en'], { type: 'region', fallback: 'none' });
const scriptNamed = new Intl.DisplayNames(['en'], { type: 'script', fallback: 'none' });

/** What a language's name says in brackets for a subtag ("United Kingdom"
 *  of "en-GB", "Simplified" of "en-Hans"). */
function bracketed(subtag) {
  const name = standard.of(`en-${subtag}`) ?? '';
  const m = /^English \((.+)\)$/.exec(name);
  return m ? m[1] : '';
}

// Every region CLDR names: the two-letter codes and the three digits of
// the world's regions ("419", "150"), but the unknown one (ZZ) and the
// pseudo-locales' (XA, XB).
const regions = {};
const letters = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';
const codes = [];
for (const a of letters) for (const b of letters) codes.push(a + b);
for (let n = 1; n < 1000; n++) codes.push(String(n).padStart(3, '0'));
for (const code of codes) {
  if (['ZZ', 'XA', 'XB'].includes(code)) continue;
  let known;
  try {
    known = regionNamed.of(code);
  } catch {
    continue;
  }
  if (!known || known === code) continue;
  const name = bracketed(code);
  if (name) regions[code] = name;
}

// Every script CLDR names, as a language's name says it ("Simplified",
// "Traditional", "Latin", "Cyrillic").
const scripts = {};
for (const a of letters) {
  for (const b of letters.toLowerCase()) {
    for (const c of letters.toLowerCase()) {
      for (const d of letters.toLowerCase()) {
        const code = a + b + c + d;
        let known;
        try {
          known = scriptNamed.of(code);
        } catch {
          continue;
        }
        if (!known || known === code || code === 'Zzzz' || code === 'Zyyy' || code === 'Zinh') continue;
        const name = bracketed(code);
        if (name) scripts[code] = name;
      }
    }
  }
}

// The regions a code in brackets after a language's name names
// ("Português (BR)"): chino-web's REGION_CODES, the regions a title may
// name, and of them those that are the language's own - where it is the
// language the region speaks most (likely subtags) or where CLDR names a
// regional form of it ("Canadian French").
const REGION_CODES = [
  'US', 'GB', 'AU', 'CA', 'NZ', 'IE', 'IN', 'ZA', 'SG', 'PH', 'MY', 'NG', 'KE', 'BR', 'PT', 'AO', 'MZ',
  'ES', 'MX', 'AR', 'CO', 'CL', 'PE', 'VE', 'UY', 'CU', 'BO', 'EC', 'PY', 'DO', 'GT', 'HN', 'NI', 'PA',
  'SV', 'CR', 'PR', '419', 'FR', 'BE', 'CH', 'LU', 'MC', 'DE', 'AT', 'LI', 'NL', 'SR', 'IT', 'SM',
  'CN', 'TW', 'HK', 'MO', 'JP', 'KR', 'KP', 'MN', 'RU', 'UA', 'BY', 'KZ', 'UZ', 'SE', 'FI', 'NO', 'DK',
  'IS', 'EE', 'LV', 'LT', 'PL', 'CZ', 'SK', 'HU', 'RO', 'MD', 'BG', 'RS', 'ME', 'BA', 'HR', 'SI', 'MK',
  'AL', 'XK', 'GR', 'CY', 'TR', 'GE', 'AM', 'AZ', 'IL', 'SA', 'EG', 'AE', 'MA', 'DZ', 'TN', 'LB', 'JO',
  'IQ', 'SY', 'KW', 'QA', 'BH', 'OM', 'YE', 'LY', 'SD', 'IR', 'AF', 'PK', 'BD', 'LK', 'NP', 'TH', 'VN',
  'ID', 'KH', 'LA', 'MM', '150',
];

function intlName(tag, locale, form) {
  try {
    return new Intl.DisplayNames([locale], { type: 'language', languageDisplay: form, fallback: 'none' }).of(tag) ?? '';
  } catch {
    return '';
  }
}

const keyOf = new Map();
for (const row of table.trim().split('\n')) {
  const [one, t, b] = row.split(/\s+/);
  const key = one !== '-' ? one : t;
  for (const code of [one, t, b]) if (code && code !== '-') keyOf.set(code, key);
}

/** Lower-cased, without its accents: "Curaçao" is "curacao" (packager.py's
 *  _folded does the same). */
function folded(text) {
  return text.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().trim();
}

// The regions a title may name in words, by their English names, long and
// short: "brazil" → "BR", "uk" → "GB", and "us" → "US", whose short name is
// its code (chino-web's regionNamesIn takes every name Intl gives).
const regionWords = {};
for (const style of ['long', 'short']) {
  const names = new Intl.DisplayNames(['en'], { type: 'region', style, fallback: 'none' });
  for (const code of REGION_CODES) {
    const name = names.of(code);
    if (name && !(folded(name) in regionWords)) regionWords[folded(name)] = code;
  }
}

/** The name without the word or words `word`. */
function withoutWords(name, word) {
  return ` ${name} `.replace(` ${word} `, ' ').replace(/\s+/g, ' ').trim();
}

// The English names CLDR's dialect form gives a language's regional forms
// ("brazilian portuguese", "flemish"), and each without the language's
// name, the adjective a title puts in brackets after it ("brazilian").
const dialects = {};
for (const base of bases) {
  const ownName = folded(intlName(base, 'en', 'standard'));
  const map = {};
  for (const region of REGION_CODES) {
    const tag = `${base}-${region}`;
    const dialect = intlName(tag, 'en', 'dialect');
    if (!dialect || dialect === intlName(tag, 'en', 'standard')) continue;
    const name = folded(dialect);
    if (!(name in map)) map[name] = region;
    const adjective = ownName ? withoutWords(name, ownName) : '';
    if (adjective && adjective !== name && !(adjective in map)) map[adjective] = region;
  }
  if (Object.keys(map).length) dialects[base] = map;
}

const own = {};
for (const base of bases) {
  const found = new Set();
  for (const locale of [base, 'en']) {
    for (const region of REGION_CODES) {
      const tag = `${base}-${region}`;
      const dialect = intlName(tag, locale, 'dialect');
      if (dialect && dialect !== intlName(tag, locale, 'standard')) found.add(region);
    }
  }
  for (const region of REGION_CODES) {
    let language = '';
    try {
      language = new Intl.Locale(`und-${region}`).maximize().language;
    } catch {
      continue;
    }
    if ((keyOf.get(language) ?? language) === base) found.add(region);
  }
  if (found.size) own[base] = [...found].sort();
}

// What each language is called in itself, as Intl names it in the
// language's own locale ("suomi" for "fi", "Indonesia" for "id", "ไทย" for
// "th"): a title that is only this names the language again, never a
// region of the same name (chino-web's namesLanguage, read before any
// region). None where Intl has no locale for the language, which would
// name it in the machine's own language instead.
function localesOf(base) {
  return (base === 'zh' ? ['zh-Hans', 'zh-Hant'] : [base]).filter(
    (locale) => Intl.DisplayNames.supportedLocalesOf([locale]).length,
  );
}
const ownNames = {};
for (const base of bases) {
  const names = [];
  for (const locale of localesOf(base)) {
    const name = intlName(base, locale, 'standard');
    if (name && !names.includes(name)) names.push(name);
  }
  if (names.length) ownNames[base] = names;
}

// A language's own regions by their names in the language itself, long and
// short, where the English name is another: "brasil" → "BR" for "pt",
// "latinoamerica" → "419" for "es", "台灣" → "TW" for "zh".
const ownWords = {};
for (const [base, list] of Object.entries(own)) {
  const map = {};
  for (const locale of base === 'zh' ? ['zh-Hans', 'zh-Hant'] : [base]) {
    for (const style of ['long', 'short']) {
      let names;
      try {
        names = new Intl.DisplayNames([locale], { type: 'region', style, fallback: 'none' });
      } catch {
        continue;
      }
      for (const code of list) {
        const name = names.of(code);
        if (!name || name === code) continue;
        const word = folded(name);
        if (!(word in regionWords) && !(word in map)) map[word] = code;
      }
    }
  }
  if (Object.keys(map).length) ownWords[base] = map;
}

const py = (s) => JSON.stringify(s);
const lines = [];
// CLDR's names hold letters ruff takes for others ("Côte d’Ivoire", Cyrillic).
lines.push('# ruff: noqa: RUF001');
lines.push('"""The names a rendition\'s NAME gives a language\'s script and region,');
lines.push('the regions that are a language\'s own, and the names a title may give a');
lines.push('language, a region or a regional form of it: CLDR\'s, as the clients\'');
lines.push('Intl.DisplayNames gives them (languageDisplay "standard"), written by');
lines.push('scripts/regions.mjs. Do not edit by hand.');
lines.push('');
lines.push(`ICU ${process.versions.icu}, CLDR ${process.versions.cldr}. The names are Unicode CLDR data,`);
lines.push('Copyright Unicode, Inc., under the Unicode License v3:');
lines.push('https://www.unicode.org/license.txt');
lines.push('"""');
lines.push('');
lines.push('# A region as a language\'s name says it in brackets: "Portuguese (Brazil)".');
lines.push('REGION_NAMES = {');
for (const [code, name] of Object.entries(regions).sort(([a], [b]) => (a < b ? -1 : 1))) {
  lines.push(`    ${py(code)}: ${py(name)},`);
}
lines.push('}');
lines.push('');
lines.push('# A script as a language\'s name says it in brackets: "Chinese (Simplified)".');
lines.push('SCRIPT_NAMES = {');
for (const [code, name] of Object.entries(scripts).sort(([a], [b]) => (a < b ? -1 : 1))) {
  lines.push(`    ${py(code)}: ${py(name)},`);
}
lines.push('}');
lines.push('');
lines.push('# The regions a code in brackets after a language\'s name names, by the');
lines.push('# language\'s key (639-1, else 639-2/T): the regions it is the most spoken');
lines.push('# language of, and those CLDR names a regional form of it for.');
lines.push('LANGUAGE_REGIONS = {');
for (const [base, list] of Object.entries(own).sort(([a], [b]) => (a < b ? -1 : 1))) {
  lines.push(`    ${py(base)}: frozenset(${py(list.join(' '))}.split()),`);
}
lines.push('}');
lines.push('');
lines.push('# The regions a title may name in words, by their English names, long and');
lines.push('# short, lower-cased and without accents: "Portuguese (Brazil)", "English (UK)".');
lines.push('REGION_WORDS = {');
for (const [name, code] of Object.entries(regionWords).sort(([a], [b]) => (a < b ? -1 : 1))) {
  lines.push(`    ${py(name)}: ${py(code)},`);
}
lines.push('}');
lines.push('');
lines.push('# What a language is called in itself, by the language\'s key: "suomi",');
lines.push('# "Indonesia". A title that is only this names the language, not a region.');
lines.push('OWN_NAMES = {');
for (const [base, names] of Object.entries(ownNames).sort(([a], [b]) => (a < b ? -1 : 1))) {
  lines.push(`    ${py(base)}: (${names.map(py).join(', ')},),`);
}
lines.push('}');
lines.push('');
lines.push('# A language\'s own regions by their names in the language, long and short,');
lines.push('# lower-cased and without accents, by the language\'s key, where English names');
lines.push('# them otherwise: "Português (Brasil)", "Español (Latinoamérica)".');
lines.push('OWN_REGION_WORDS = {');
for (const [base, map] of Object.entries(ownWords).sort(([a], [b]) => (a < b ? -1 : 1))) {
  lines.push(`    ${py(base)}: {`);
  for (const [name, code] of Object.entries(map).sort(([a], [b]) => (a < b ? -1 : 1))) {
    lines.push(`        ${py(name)}: ${py(code)},`);
  }
  lines.push('    },');
}
lines.push('}');
lines.push('');
lines.push('# The English names of a language\'s regional forms and their adjectives, by');
lines.push('# the language\'s key: "Brazilian Portuguese", "Portuguese (Brazilian)".');
lines.push('DIALECTS = {');
for (const [base, map] of Object.entries(dialects).sort(([a], [b]) => (a < b ? -1 : 1))) {
  lines.push(`    ${py(base)}: {`);
  for (const [name, code] of Object.entries(map).sort(([a], [b]) => (a < b ? -1 : 1))) {
    lines.push(`        ${py(name)}: ${py(code)},`);
  }
  lines.push('    },');
}
lines.push('}');
process.stdout.write(lines.join('\n') + '\n');
