//! Run-length encoding of text.
//!
//! `encode` turns every maximal run of `n` equal characters into the decimal
//! number `n` followed by the character, so `"aaab"` becomes `"3a1b"` and a
//! run of twelve `x` becomes `"12x"`. Runs are never split. Characters are
//! Unicode scalar values, so `"éé"` becomes `"2é"`. Digits in the input are
//! not supported by the format.
//!
//! `decode` reverses `encode`. Counts may have any number of digits. It returns
//! an error for a count with no character after it, a character with no count
//! before it, or a count of zero.

pub fn encode(input: &str) -> String {
    let mut out = String::new();
    let mut chars = input.chars().peekable();
    while let Some(c) = chars.next() {
        let mut count = 1usize;
        while chars.peek() == Some(&c) {
            chars.next();
            count += 1;
        }
        out.push_str(&count.to_string());
        out.push(c);
    }
    out
}

pub fn decode(input: &str) -> Result<String, String> {
    let mut out = String::new();
    let mut count = String::new();
    for c in input.chars() {
        if c.is_ascii_digit() {
            count.push(c);
            continue;
        }
        if count.is_empty() {
            return Err(format!("missing count before {:?}", c));
        }
        let n: usize = count.parse().map_err(|_| format!("bad count {:?}", count))?;
        if n == 0 {
            return Err("zero count".to_string());
        }
        for _ in 0..n {
            out.push(c);
        }
        count.clear();
    }
    if !count.is_empty() {
        return Err(format!("count {:?} has no character", count));
    }
    Ok(out)
}
