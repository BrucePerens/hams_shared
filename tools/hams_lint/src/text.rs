// SPDX-License-Identifier: AGPL-3.0-or-later
//! Python text semantics the legacy checkers rely on, reproduced exactly so a migrated check reports
//! the same line numbers and the same partial results as its Python twin:
//!
//! * `for line in open(path, encoding="utf-8")`: universal newlines (`\n`, `\r\n` and `\r` each end a
//!   line and are read as `\n`), decoded in 8192-byte chunks. A `UnicodeDecodeError` is raised while
//!   decoding the chunk that holds the bad byte, so the lines that ended in earlier chunks were already
//!   handed to the checker (and their findings kept). [`strict_lines`] returns those lines and the error.
//! * `open(path, errors="replace").read()`: invalid bytes become U+FFFD; [`lossy_text`].
//! * `str.splitlines()`: also splits on `\v`, `\f`, `\x1c`-`\x1e`, `\x85`, U+2028 and U+2029; [`splitlines`].
//! * `str.strip()` / `str.isspace()`: Python's whitespace set includes `\x1c`-`\x1f`; [`py_strip`].

use std::borrow::Cow;

/// Python's `io.TextIOWrapper._CHUNK_SIZE`.
pub const CHUNK: usize = 8192;

/// A decoding failure, described the way Python's `UnicodeDecodeError` prints it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DecodeError {
    /// Byte offset of the first undecodable byte in the file.
    pub offset: usize,
    pub message: String,
}

fn decode_error(bytes: &[u8], e: &std::str::Utf8Error, chunk_start: usize) -> DecodeError {
    let p = e.valid_up_to();
    let pos = p - chunk_start;
    let reason;
    let mut span = 1;
    match e.error_len() {
        None => {
            reason = "unexpected end of data";
            span = bytes.len() - p;
        }
        Some(_) => {
            let b = bytes[p];
            reason = if (0x80..=0xBF).contains(&b) || b == 0xC0 || b == 0xC1 || b >= 0xF5 {
                "invalid start byte"
            } else {
                "invalid continuation byte"
            };
        }
    }
    let message = if span <= 1 {
        format!(
            "'utf-8' codec can't decode byte 0x{:02x} in position {}: {}",
            bytes[p], pos, reason
        )
    } else {
        format!(
            "'utf-8' codec can't decode bytes in position {}-{}: {}",
            pos,
            pos + span - 1,
            reason
        )
    };
    DecodeError { offset: p, message }
}

/// One line as Python's universal-newline iteration yields it: the text plus a trailing `\n` when the
/// line had any terminator.
pub type PyLine<'a> = Cow<'a, str>;

/// Splits decoded text into universal-newline lines, each with its terminator translated to `\n`.
/// `limit`: only lines whose terminator lies wholly before this byte offset are returned (plus, when
/// `limit` is the text length, a final unterminated line).
fn universal_lines(text: &str, limit: usize, at_eof: bool) -> Vec<PyLine<'_>> {
    let bytes = text.as_bytes();
    let mut out = Vec::new();
    let mut start = 0;
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'\n' => {
                if i + 1 > limit {
                    return out;
                }
                out.push(Cow::Borrowed(&text[start..=i]));
                i += 1;
                start = i;
            }
            b'\r' => {
                let crlf = i + 1 < bytes.len() && bytes[i + 1] == b'\n';
                // Python holds a `\r` at the end of a decoded chunk until it sees the next character.
                let end = if crlf { i + 2 } else { i + 1 };
                if end > limit || (!crlf && end == limit && !at_eof) {
                    return out;
                }
                let mut owned = String::with_capacity(end - start);
                owned.push_str(&text[start..i]);
                owned.push('\n');
                out.push(Cow::Owned(owned));
                i = end;
                start = i;
            }
            _ => i += 1,
        }
    }
    if start < bytes.len() && at_eof {
        out.push(Cow::Borrowed(&text[start..]));
    }
    out
}

/// `for line in open(path, encoding="utf-8")`: the lines Python yields before it raises, and the
/// error it raises (None when the whole file decodes).
pub fn strict_lines(bytes: &[u8]) -> (Vec<PyLine<'_>>, Option<DecodeError>) {
    match std::str::from_utf8(bytes) {
        Ok(text) => (universal_lines(text, text.len(), true), None),
        Err(e) => {
            let p = e.valid_up_to();
            let cutoff = (p / CHUNK) * CHUNK;
            // The prefix up to the last complete character before `cutoff` is valid UTF-8.
            let mut valid = cutoff;
            while valid > 0 && std::str::from_utf8(&bytes[..valid]).is_err() {
                valid -= 1;
            }
            let text = std::str::from_utf8(&bytes[..valid]).unwrap_or("");
            (
                universal_lines(text, valid, false),
                Some(decode_error(bytes, &e, cutoff)),
            )
        }
    }
}

/// `open(path, encoding="utf-8", errors="replace").read()`.
pub fn lossy_text(bytes: &[u8]) -> Cow<'_, str> {
    match String::from_utf8_lossy(bytes) {
        Cow::Borrowed(text) => translate_newlines(text),
        Cow::Owned(text) => Cow::Owned(translate_newlines(&text).into_owned()),
    }
}

/// `\r\n` and `\r` read as `\n` (Python's universal newlines on read()).
pub fn translate_newlines(text: &str) -> Cow<'_, str> {
    if !text.contains('\r') {
        return Cow::Borrowed(text);
    }
    Cow::Owned(text.replace("\r\n", "\n").replace('\r', "\n"))
}

fn is_line_boundary(c: char) -> bool {
    matches!(
        c,
        '\n' | '\r'
            | '\x0b'
            | '\x0c'
            | '\x1c'
            | '\x1d'
            | '\x1e'
            | '\u{85}'
            | '\u{2028}'
            | '\u{2029}'
    )
}

/// Python `str.splitlines()` (no ends kept).
pub fn splitlines(text: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        if is_line_boundary(c) {
            out.push(&text[start..i]);
            let mut next = i + c.len_utf8();
            if c == '\r' {
                if let Some(&(j, '\n')) = chars.peek() {
                    chars.next();
                    next = j + 1;
                }
            }
            start = next;
        }
    }
    if start < text.len() {
        out.push(&text[start..]);
    }
    out
}

/// Python `str.isspace()` for one character.
pub fn py_isspace(c: char) -> bool {
    c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)
}

/// Python `str.strip()`.
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(py_isspace)
}

/// Python `str.lstrip()`.
pub fn py_lstrip(s: &str) -> &str {
    s.trim_start_matches(py_isspace)
}

/// Python `os.path.splitext(name)[1]` for a file name (leading dots do not start an extension).
pub fn py_ext(name: &str) -> &str {
    match name.rfind('.') {
        Some(dot) if name[..dot].chars().any(|c| c != '.') => &name[dot..],
        _ => "",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn universal_newlines_translate_and_number_like_python() {
        let (lines, err) = strict_lines(b"a\r\nb\rc\nd");
        assert!(err.is_none());
        assert_eq!(lines, vec!["a\n", "b\n", "c\n", "d"]);
    }

    #[test]
    fn decode_error_keeps_only_lines_of_earlier_chunks() {
        let mut data = Vec::new();
        for _ in 0..1000 {
            data.extend_from_slice(b"0123456789\n"); // 11 000 bytes: crosses one chunk boundary
        }
        data.push(0xff);
        let (lines, err) = strict_lines(&data);
        let err = err.expect("decode error");
        assert_eq!(err.offset, 11000);
        assert_eq!(lines.len(), CHUNK / 11);
        assert!(err.message.contains("invalid start byte"));
        assert!(err.message.contains(&format!("position {}", 11000 - CHUNK)));
    }

    #[test]
    fn decode_error_in_first_chunk_yields_no_lines() {
        let (lines, err) = strict_lines(b"ok\n\xffbad\n");
        assert!(lines.is_empty());
        assert_eq!(
            err.unwrap().message,
            "'utf-8' codec can't decode byte 0xff in position 3: invalid start byte"
        );
    }

    #[test]
    fn truncated_sequence_at_end_is_unexpected_end_of_data() {
        let err = strict_lines(b"ab\xe2\x82").1.expect("decode error");
        assert_eq!(
            err.message,
            "'utf-8' codec can't decode bytes in position 2-3: unexpected end of data"
        );
    }

    #[test]
    fn splitlines_matches_python_boundaries() {
        assert_eq!(
            splitlines("a\x0bb\x0cc\u{2028}d\r\ne\n"),
            vec!["a", "b", "c", "d", "e"]
        );
        assert_eq!(splitlines(""), Vec::<&str>::new());
        assert_eq!(splitlines("\n\n"), vec!["", ""]);
    }

    #[test]
    fn python_whitespace_and_extension_rules() {
        assert_eq!(py_strip("\x1c EOF \n"), "EOF");
        assert_eq!(py_lstrip("  # x"), "# x");
        assert_eq!(py_ext(".bashrc"), "");
        assert_eq!(py_ext("a.tar.gz"), ".gz");
        assert_eq!(py_ext("Makefile"), "");
        assert_eq!(py_ext("..x"), "");
    }
}
