from karaoke_shared.alignment.document import document_to_syllable_timings
from karaoke_shared.models.alignment import (
    AlignmentDocument,
    AlignmentLine,
    AlignmentSection,
    AlignmentSyllable,
    AlignmentWord,
)


def test_document_to_syllable_timings_preserves_word_spaces_and_line_breaks() -> None:
    document = AlignmentDocument(
        sections=[AlignmentSection(id="section_1", title="Main", line_ids=["line_1", "line_2"])],
        lines=[
            AlignmentLine(id="line_1", text="See those leaders", start=0.0, end=1.5, word_ids=["word_1", "word_2", "word_3"]),
            AlignmentLine(id="line_2", text="start to frown", start=1.5, end=3.0, word_ids=["word_4", "word_5", "word_6"]),
        ],
        words=[
            AlignmentWord(id="word_1", text="See", start=0.0, end=0.4, line_id="line_1", syllable_ids=["syl_1"]),
            AlignmentWord(id="word_2", text="those", start=0.4, end=0.9, line_id="line_1", syllable_ids=["syl_2"]),
            AlignmentWord(id="word_3", text="leaders", start=0.9, end=1.5, line_id="line_1", syllable_ids=["syl_3", "syl_4"]),
            AlignmentWord(id="word_4", text="start", start=1.5, end=2.0, line_id="line_2", syllable_ids=["syl_5"]),
            AlignmentWord(id="word_5", text="to", start=2.0, end=2.3, line_id="line_2", syllable_ids=["syl_6"]),
            AlignmentWord(id="word_6", text="frown", start=2.3, end=3.0, line_id="line_2", syllable_ids=["syl_7"]),
        ],
        syllables=[
            AlignmentSyllable(id="syl_1", text="See", start=0.0, end=0.4, word_id="word_1", line_id="line_1"),
            AlignmentSyllable(id="syl_2", text="those", start=0.4, end=0.9, word_id="word_2", line_id="line_1"),
            AlignmentSyllable(id="syl_3", text="lead", start=0.9, end=1.2, word_id="word_3", line_id="line_1"),
            AlignmentSyllable(id="syl_4", text="ers", start=1.2, end=1.5, word_id="word_3", line_id="line_1"),
            AlignmentSyllable(id="syl_5", text="start", start=1.5, end=2.0, word_id="word_4", line_id="line_2"),
            AlignmentSyllable(id="syl_6", text="to", start=2.0, end=2.3, word_id="word_5", line_id="line_2"),
            AlignmentSyllable(id="syl_7", text="frown", start=2.3, end=3.0, word_id="word_6", line_id="line_2"),
        ],
    )

    timings = document_to_syllable_timings(document)

    assert [item.syllable for item in timings] == [
        "See",
        " those",
        " lead",
        "ers",
        "\nstart",
        " to",
        " frown",
    ]
