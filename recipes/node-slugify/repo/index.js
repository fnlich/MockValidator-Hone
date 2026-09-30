'use strict';

/**
 * slugify(text, { maxLength })
 *
 * 1. Unicode-normalize to NFD and drop combining marks ("Café" -> "Cafe").
 * 2. Lowercase.
 * 3. Replace every run of characters other than a-z and 0-9 with ONE "-".
 * 4. Remove leading and trailing "-".
 * 5. If maxLength is given and the slug is longer, cut it to at most
 *    maxLength characters at a "-" boundary when one exists within the limit
 *    (otherwise cut exactly at maxLength), and never leave a trailing "-".
 */
function slugify(text, options = {}) {
  let s = String(text).normalize('NFD').replace(/[\u0300-\u036f]/g, '');
  s = s.toLowerCase();
  s = s.replace(/[^a-z0-9]+/g, '-');
  s = s.replace(/^-+|-+$/g, '');
  const max = options.maxLength;
  if (max !== undefined && s.length > max) {
    const cut = s.slice(0, max + 1);
    const boundary = cut.lastIndexOf('-');
    s = boundary > 0 ? cut.slice(0, boundary) : s.slice(0, max);
    s = s.replace(/-+$/, '');
  }
  return s;
}

module.exports = { slugify };
