use serde::{Deserialize, Serialize};
use std::cmp::Ordering;
use std::collections::BTreeSet;
use std::ffi::{CStr, CString};
use std::os::raw::c_char;

const STOP_WORDS: &[&str] = &[
    "需要关注",
    "信息不足",
    "待核实",
    "page",
    "latest",
    "报告",
    "指出",
    "导致",
    "发生",
    "需要",
    "核对",
    "分析",
    "请求",
    "检索",
    "知识库",
];

#[no_mangle]
pub extern "C" fn accel_tokenize_text(input: *const c_char) -> *mut c_char {
    with_c_input(input, |text| {
        Some(
            tokenize_text(text)
                .into_iter()
                .collect::<Vec<String>>()
                .join("\n"),
        )
    })
}

#[no_mangle]
pub extern "C" fn accel_tokenize_batch(input: *const c_char) -> *mut c_char {
    with_c_input(input, |raw| {
        let texts: Vec<String> = serde_json::from_str(raw).ok()?;
        serde_json::to_string(
            &texts
                .iter()
                .map(|item| tokenize_text(item).into_iter().collect::<Vec<String>>())
                .collect::<Vec<Vec<String>>>(),
        )
        .ok()
    })
}

#[no_mangle]
pub extern "C" fn accel_score_records(input: *const c_char) -> *mut c_char {
    with_c_input(input, |raw| {
        let payload: ScorePayload = serde_json::from_str(raw).ok()?;
        let token_count = payload.query_tokens.len().min(8).max(1) as f32;
        let mut ranked: Vec<ScoredResult> = payload
            .candidates
            .iter()
            .enumerate()
            .filter_map(|(index, item)| {
                let score = compute_index_score(item, &payload.query_tokens, token_count);
                (score >= payload.min_score).then(|| ScoredResult {
                    id: item.id.clone(),
                    record_type: item.record_type.clone(),
                    score,
                    index,
                })
            })
            .collect();

        ranked.sort_by(|left, right| {
            right
                .score
                .partial_cmp(&left.score)
                .unwrap_or(Ordering::Equal)
                .then_with(|| left.index.cmp(&right.index))
        });
        let limit = payload.limit.max(1);
        ranked.truncate(limit);

        serde_json::to_string(
            &ranked
                .iter()
                .map(|item| OutputScore {
                    id: item.id.clone(),
                    record_type: item.record_type.clone(),
                    score: item.score,
                })
                .collect::<Vec<OutputScore>>(),
        )
        .ok()
    })
}

#[no_mangle]
pub extern "C" fn accel_extract_json_candidates(input: *const c_char) -> *mut c_char {
    with_c_input(input, |raw| serde_json::to_string(&extract_json_candidates(raw)).ok())
}

#[no_mangle]
pub extern "C" fn accel_free_string(ptr: *mut c_char) {
    if ptr.is_null() {
        return;
    }
    unsafe {
        let _ = CString::from_raw(ptr);
    }
}

fn tokenize_text(text: &str) -> BTreeSet<String> {
    let normalized = text
        .split_whitespace()
        .collect::<Vec<&str>>()
        .join(" ")
        .to_lowercase();
    let chars: Vec<char> = normalized.chars().collect();
    let mut index = 0usize;
    let mut tokens: BTreeSet<String> = BTreeSet::new();

    while index < chars.len() {
        let current = chars[index];
        if is_cjk(current) {
            let start = index;
            while index < chars.len() && is_cjk(chars[index]) {
                index += 1;
            }
            let segment: String = chars[start..index].iter().collect();
            push_cjk_tokens(&segment, &mut tokens);
            continue;
        }

        if is_ascii_token_char(current) {
            let start = index;
            while index < chars.len() && is_ascii_token_char(chars[index]) {
                index += 1;
            }
            let segment: String = chars[start..index].iter().collect();
            if segment.len() >= 2
                && !segment.chars().all(|item| item.is_ascii_digit())
                && !STOP_WORDS.contains(&segment.as_str())
            {
                tokens.insert(segment);
            }
            continue;
        }

        index += 1;
    }

    tokens
}

fn extract_json_candidates(content: &str) -> Vec<String> {
    let mut candidates: Vec<String> = Vec::new();
    let trimmed = content.trim();

    for fenced in extract_fenced_candidates(trimmed) {
        push_json_variants(&mut candidates, &fenced);
    }

    if trimmed.starts_with('{') && trimmed.ends_with('}') {
        push_json_variants(&mut candidates, trimmed);
    }

    for candidate in extract_balanced_candidates(trimmed) {
        push_json_variants(&mut candidates, &candidate);
    }

    candidates
}

fn with_c_input<F>(input: *const c_char, handler: F) -> *mut c_char
where
    F: FnOnce(&str) -> Option<String> + std::panic::UnwindSafe,
{
    let output = std::panic::catch_unwind(|| {
        if input.is_null() {
            return None;
        }

        let raw = unsafe { CStr::from_ptr(input) };
        let Ok(text) = raw.to_str() else {
            return None;
        };
        handler(text)
    });

    match output
        .ok()
        .flatten()
        .and_then(|item| CString::new(item).ok())
    {
        Some(value) => value.into_raw(),
        None => std::ptr::null_mut(),
    }
}

fn compute_index_score(
    candidate: &ScoreCandidate,
    query_tokens: &[String],
    token_count: f32,
) -> f32 {
    let coverage_score = (candidate.state.match_count as f32 / token_count) * 0.45;
    let tf_score = (candidate.state.tf_sum / 6.0).min(0.22);
    let title_score = candidate.state.title_hits.min(3) as f32 * 0.06;
    let field_score = if !candidate.category.is_empty() || !candidate.rule_type.is_empty() {
        0.05
    } else {
        0.0
    };
    let semantic_score = score_record(candidate, query_tokens).min(0.35);
    let bonus_score = candidate.state.bonus.min(0.18);
    round4(
        (coverage_score + tf_score + title_score + field_score + semantic_score + bonus_score)
            .min(1.0),
    )
}

fn score_record(candidate: &ScoreCandidate, query_tokens: &[String]) -> f32 {
    let title = candidate.title.to_lowercase();
    let tags = candidate.tags.join(" ").to_lowercase();
    let rule_type = candidate.rule_type.to_lowercase();
    let scenarios = candidate.scenarios.join(" ").to_lowercase();
    let liability_subjects = candidate.liability_subjects.join(" ").to_lowercase();
    let content = candidate.content.to_lowercase();
    let haystack = [
        title.as_str(),
        tags.as_str(),
        rule_type.as_str(),
        scenarios.as_str(),
        liability_subjects.as_str(),
        content.as_str(),
    ]
    .join(" ");

    let mut hits = 0usize;
    let mut title_hits = 0usize;
    for token in query_tokens {
        if haystack.contains(token) {
            hits += 1;
            if title.contains(token) {
                title_hits += 1;
            }
        }
    }

    if hits == 0 {
        return 0.0;
    }

    let coverage = hits as f32 / query_tokens.len().min(8).max(1) as f32;
    let mut score = coverage * 0.55;
    score += title_hits.min(2) as f32 * 0.08;
    if !candidate.category.is_empty() || !candidate.rule_type.is_empty() {
        score += 0.05;
    }
    if !candidate.scenarios.is_empty() || !candidate.liability_subjects.is_empty() {
        score += 0.04;
    }
    round4(score.min(1.0))
}

fn round4(value: f32) -> f32 {
    (value * 10_000.0).round() / 10_000.0
}

fn extract_fenced_candidates(content: &str) -> Vec<String> {
    let mut candidates = Vec::new();
    let mut cursor = 0usize;

    while let Some(start_offset) = content[cursor..].find("```") {
        let fence_start = cursor + start_offset;
        let body_start = fence_start + 3;
        let Some(end_offset) = content[body_start..].find("```") else {
            break;
        };
        let fence_end = body_start + end_offset;
        let block = content[body_start..fence_end].trim();
        let candidate = if let Some((first_line, rest)) = block.split_once('\n') {
            let header = first_line.trim().to_lowercase();
            if header.is_empty() || header == "json" {
                rest.trim()
            } else {
                ""
            }
        } else {
            let header = block.trim().to_lowercase();
            if header.starts_with('{') && header.ends_with('}') {
                block
            } else {
                ""
            }
        };

        if !candidate.is_empty() && candidate.contains('{') && candidate.contains('}') {
            push_unique_string(&mut candidates, candidate.to_string());
        }
        cursor = fence_end + 3;
    }

    candidates
}

fn extract_balanced_candidates(content: &str) -> Vec<String> {
    let mut candidates = Vec::new();
    for (index, ch) in content.char_indices() {
        if ch != '{' {
            continue;
        }
        if let Some(end) = find_balanced_json_end(content, index) {
            push_unique_string(&mut candidates, content[index..=end].to_string());
        } else {
            push_unique_string(&mut candidates, content[index..].to_string());
        }
    }
    candidates.sort_by(|left, right| right.len().cmp(&left.len()));
    candidates
}

fn find_balanced_json_end(content: &str, start: usize) -> Option<usize> {
    let mut depth = 0usize;
    let mut in_string = false;
    let mut escaped = false;
    let mut quote_char = '"';

    for (index, ch) in content.char_indices().skip_while(|(idx, _)| *idx < start) {
        if in_string {
            if escaped {
                escaped = false;
                continue;
            }
            if ch == '\\' {
                escaped = true;
                continue;
            }
            if ch == quote_char {
                in_string = false;
            }
            continue;
        }

        if ch == '"' || ch == '\'' {
            in_string = true;
            quote_char = ch;
            continue;
        }
        if ch == '{' {
            depth += 1;
            continue;
        }
        if ch == '}' {
            depth = depth.saturating_sub(1);
            if depth == 0 {
                return Some(index);
            }
        }
    }

    None
}

fn push_json_variants(target: &mut Vec<String>, candidate: &str) {
    let normalized = normalize_json_like_text(candidate);
    if normalized.is_empty() {
        return;
    }

    push_unique_string(target, normalized.clone());
    let repaired = remove_trailing_commas(&normalized);
    if repaired != normalized {
        push_unique_string(target, repaired.clone());
    }

    let balanced = append_missing_closers(&repaired);
    if balanced != repaired {
        push_unique_string(target, balanced);
    }

    let normalized_balanced = append_missing_closers(&normalized);
    if normalized_balanced != normalized {
        push_unique_string(target, normalized_balanced);
    }
}

fn normalize_json_like_text(candidate: &str) -> String {
    let mut normalized = candidate.trim().trim_start_matches('\u{feff}').trim().to_string();
    if normalized
        .get(..4)
        .is_some_and(|prefix| prefix.eq_ignore_ascii_case("json"))
    {
        normalized = normalized
            .get(4..)
            .unwrap_or_default()
            .trim_start()
            .to_string();
    }

    normalized
        .replace('“', "\"")
        .replace('”', "\"")
        .replace('‘', "'")
        .replace('’', "'")
        .replace('：', ":")
        .replace('，', ",")
        .replace('（', "(")
        .replace('）', ")")
}

fn remove_trailing_commas(candidate: &str) -> String {
    let chars: Vec<char> = candidate.chars().collect();
    let mut output = String::with_capacity(candidate.len());
    let mut index = 0usize;
    let mut in_string = false;
    let mut escaped = false;
    let mut quote_char = '"';

    while index < chars.len() {
        let ch = chars[index];
        if in_string {
            output.push(ch);
            if escaped {
                escaped = false;
            } else if ch == '\\' {
                escaped = true;
            } else if ch == quote_char {
                in_string = false;
            }
            index += 1;
            continue;
        }

        if ch == '"' || ch == '\'' {
            in_string = true;
            quote_char = ch;
            output.push(ch);
            index += 1;
            continue;
        }

        if ch == ',' {
            let mut lookahead = index + 1;
            while lookahead < chars.len() && chars[lookahead].is_whitespace() {
                lookahead += 1;
            }
            if lookahead < chars.len() && (chars[lookahead] == '}' || chars[lookahead] == ']') {
                index += 1;
                continue;
            }
        }

        output.push(ch);
        index += 1;
    }

    output
}

fn append_missing_closers(candidate: &str) -> String {
    let mut open_braces = 0usize;
    let mut close_braces = 0usize;
    let mut open_brackets = 0usize;
    let mut close_brackets = 0usize;
    let mut in_string = false;
    let mut escaped = false;
    let mut quote_char = '"';

    for ch in candidate.chars() {
        if in_string {
            if escaped {
                escaped = false;
                continue;
            }
            if ch == '\\' {
                escaped = true;
                continue;
            }
            if ch == quote_char {
                in_string = false;
            }
            continue;
        }

        if ch == '"' || ch == '\'' {
            in_string = true;
            quote_char = ch;
            continue;
        }

        match ch {
            '{' => open_braces += 1,
            '}' => close_braces += 1,
            '[' => open_brackets += 1,
            ']' => close_brackets += 1,
            _ => {}
        }
    }

    let mut repaired = candidate.to_string();
    if open_brackets > close_brackets {
        repaired.push_str(&"]".repeat(open_brackets - close_brackets));
    }
    if open_braces > close_braces {
        repaired.push_str(&"}".repeat(open_braces - close_braces));
    }
    repaired
}

fn push_unique_string(target: &mut Vec<String>, value: String) {
    if value.is_empty() || target.iter().any(|item| item == &value) {
        return;
    }
    target.push(value);
}

fn push_cjk_tokens(segment: &str, tokens: &mut BTreeSet<String>) {
    let char_count = segment.chars().count();
    if char_count < 2 || STOP_WORDS.contains(&segment) {
        return;
    }
    tokens.insert(segment.to_string());
    if char_count <= 4 {
        return;
    }

    let segment_chars: Vec<char> = segment.chars().collect();
    for size in [4usize, 3usize, 2usize] {
        if char_count < size {
            continue;
        }
        for offset in 0..=(char_count - size) {
            let token: String = segment_chars[offset..offset + size].iter().collect();
            if STOP_WORDS.contains(&token.as_str()) {
                continue;
            }
            tokens.insert(token);
        }
    }
}

fn is_cjk(ch: char) -> bool {
    matches!(ch as u32, 0x4E00..=0x9FFF)
}

fn is_ascii_token_char(ch: char) -> bool {
    ch.is_ascii_alphanumeric() || matches!(ch, '_' | '.' | ':' | '/' | '-')
}

#[derive(Deserialize)]
struct ScorePayload {
    query_tokens: Vec<String>,
    min_score: f32,
    limit: usize,
    candidates: Vec<ScoreCandidate>,
}

#[derive(Deserialize)]
struct ScoreCandidate {
    id: String,
    record_type: String,
    title: String,
    content: String,
    category: String,
    rule_type: String,
    tags: Vec<String>,
    scenarios: Vec<String>,
    liability_subjects: Vec<String>,
    state: ScoreState,
}

#[derive(Deserialize)]
struct ScoreState {
    match_count: usize,
    tf_sum: f32,
    title_hits: usize,
    bonus: f32,
}

struct ScoredResult {
    id: String,
    record_type: String,
    score: f32,
    index: usize,
}

#[derive(Serialize)]
struct OutputScore {
    id: String,
    record_type: String,
    score: f32,
}
