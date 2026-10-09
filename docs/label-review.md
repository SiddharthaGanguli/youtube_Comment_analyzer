# What we learned from the label sample

I read the 200 sampled comments with their video titles before comparing my
judgments with the supplied sentiment labels. Each decision has a short reason
in the worksheet. This is an AI assistant review, so the original fields for
your own labels and notes are still blank.

There are 167 provisional three-class decisions and 33 comments that need more
context. Of the provisional decisions, 137 agree with the supplied label and
30 disagree. That is 82.0% agreement among those 167 comments. The sample was
stratified by supplied class, so this is neither a population error rate nor
the model's accuracy. The 30 differences are candidates to discuss, rather than
confirmed labeling errors.

The review files are:

- `data/processed/eda/label_review_sample.csv`: source comments, unchanged labels,
  blank human-review fields, and four new assistant-review columns. DVC versions
  this worksheet and stores it on the existing S3 remote.
- `reports/label_review_report.json`: source and worksheet checksums, counts,
  disagreements, and source rows needing context. Git versions this summary.

## The rules I used

Judge the opinion expressed in the comment. A person's political position does
not decide their sentiment. Praise and support are positive; criticism,
disappointment, anger, and distress are negative. Information, technical
questions, timestamps, and requests without an opinion are usually neutral.

Read the whole comment. A crying emoji in an enthusiastic food request does not
make it negative. A technical error message does not mean the viewer dislikes
the tutorial. Praise for the teacher can coexist with criticism of a different
subject, so the target of the opinion matters.

Sarcasm, unexplained nicknames, emoji-only replies, memes, and balanced mixed
feelings often need more context. Multilingual readings also deserve a fluent
speaker's confirmation. I marked unclear cases `Uncertain`; it is a review
status, not a fourth class to introduce into training.

Three useful examples to discuss are source row 481286 (explicit praise supplied
as Neutral), 231127 (a debugging-help request supplied as Negative), and 212703
(regret supplied as Neutral). Their original comments and reasons are in the
worksheet. Some differences may reflect annotation policy rather than a simple
mistake, particularly when a comment supports one person and attacks another.

## Your review

Restore the worksheet on another checkout:

```powershell
.\.venv\Scripts\python.exe -m dvc pull data/processed/eda/label_review_sample.csv.dvc
```

Start with the 33 `needs_context` rows and 30 disagreements. Write your label in
`reviewed_sentiment` and your explanation in `review_notes`. Use Positive,
Neutral, or Negative when the meaning is clear; leave it blank when unresolved.
The assistant columns are separate so you can disagree with them.

After editing, refresh the source-verified summary and DVC pointer:

```powershell
.\.venv\Scripts\python.exe scripts/summarize_label_review.py
.\.venv\Scripts\python.exe -m dvc add data/processed/eda/label_review_sample.csv
.\.venv\Scripts\python.exe -m dvc push data/processed/eda/label_review_sample.csv.dvc
```

Commit the pointer and summary together. Rerunning the EDA notebook preserves
the existing worksheet and all its review columns. The original raw CSV and
training labels have not been edited. Human label confirmation remains pending,
so evaluation continues to describe agreement with supplied dataset labels.
