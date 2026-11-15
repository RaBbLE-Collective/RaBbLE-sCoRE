//! RaBbLE's thinking words: the gerund phrase in the divider while a turn runs, in place of a bare
//! "working". One word per turn, rotating slowly so a long turn does not sit on one phrase.

use std::time::Duration;

const BANK: &str = include_str!("thinking_words.txt");
/// A long turn moves to the next word this often.
const ROTATE: Duration = Duration::from_secs(8);

pub fn words() -> Vec<&'static str> {
    BANK.lines().map(str::trim).filter(|l| !l.is_empty() && !l.starts_with('#')).collect()
}

/// The word for turn `turn` after `elapsed`. `turn` is mixed so consecutive turns do not walk the list in order.
pub fn word(turn: u64, elapsed: Duration) -> &'static str {
    let w = words();
    if w.is_empty() {
        return "Working";
    }
    let mixed = turn.wrapping_mul(0x9E37_79B9_7F4A_7C15) >> 33;
    let step = elapsed.as_secs() / ROTATE.as_secs();
    w[((mixed + step) % w.len() as u64) as usize]
}

/// The pondering glyph: a slow bloom, or a plain spinner where the terminal cannot draw it.
pub fn spinner(elapsed: Duration, plain: bool) -> char {
    const BLOOM: [char; 8] = ['·', '✢', '✳', '✶', '✻', '✽', '✻', '✶'];
    const PLAIN: [char; 4] = ['|', '/', '-', '\\'];
    let tick = (elapsed.as_millis() / 140) as usize;
    if plain { PLAIN[tick % PLAIN.len()] } else { BLOOM[tick % BLOOM.len()] }
}

/// `12s`, `1m 05s`.
pub fn clock(d: Duration) -> String {
    let s = d.as_secs();
    if s < 60 { format!("{s}s") } else { format!("{}m {:02}s", s / 60, s % 60) }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_bank_is_clean() {
        let w = words();
        assert!(w.len() >= 40);
        assert!(w.iter().all(|l| !l.contains('—') && !l.contains('–')), "no em or en dashes");
        assert!(!w.contains(&"Working"));
    }

    #[test]
    fn a_word_holds_then_rotates() {
        assert_eq!(word(3, Duration::from_secs(0)), word(3, Duration::from_secs(7)));
        assert_ne!(word(3, Duration::from_secs(0)), word(3, Duration::from_secs(8)));
    }

    #[test]
    fn clock_reads_naturally() {
        assert_eq!((clock(Duration::from_secs(9)), clock(Duration::from_secs(65))), ("9s".into(), "1m 05s".into()));
    }
}
