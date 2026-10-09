// Handwritten sample comments for the design preview. No YouTube data is fetched.
const rows = [
  ["Maya R.", "Finally a project walkthrough that explains the why, not just the code. Loved this!", "Positive", .94, 142, 1],
  ["Arjun K.", "The train/test split explanation made everything click for me. Thank you 🙌", "Positive", .91, 98, 2],
  ["Alex T.", "Great start, but the audio gets really quiet halfway through.", "Negative", .56, 76, 3],
  ["Priya S.", "Can you share the GitHub repository and the dataset link?", "Neutral", .83, 64, 3],
  ["Daniel M.", "I followed along and got my first model working. Such a good feeling!", "Positive", .96, 57, 4],
  ["Nora L.", "The notebook is so easy to follow. Please make more videos like this.", "Positive", .93, 45, 5],
  ["Sam W.", "Would the same pipeline work with comments in Hindi?", "Neutral", .78, 38, 5],
  ["Riya P.", "Not bad at all. The data cleaning section was actually my favourite part.", "Positive", .58, 32, 6],
  ["Chris B.", "This moved too fast for a beginner. I couldn't follow the configuration section.", "Negative", .89, 29, 7],
  ["Fatima A.", "The little checks for duplicate comments saved me a lot of debugging. Thanks!", "Positive", .92, 27, 1],
  ["Leo C.", "What version of Python are you using here?", "Neutral", .88, 23, 2],
  ["Ishaan D.", "Clean code and clear explanations. Subscribed for the next part.", "Positive", .95, 21, 3],
  ["Grace H.", "I wish you had explained where the labels came from. That part felt incomplete.", "Negative", .86, 18, 4],
  ["Omar J.", "I like that you checked for leakage before training. Really useful example.", "Positive", .9, 17, 5],
  ["Sofia N.", "The video is 12 minutes long, and the model section starts around minute 8.", "Neutral", .93, 14, 6],
  ["Dev P.", "Pretty good, though I had to pause a few times to keep up.", "Positive", .55, 12, 7],
  ["Emma F.", "Exactly what I needed for my college project. The diagrams really helped.", "Positive", .92, 11, 1],
  ["Kabir V.", "Does logistic regression need the comments to be in English?", "Neutral", .79, 9, 2],
  ["Ava G.", "The background music was distracting. Please turn it down in the next one.", "Negative", .87, 8, 3],
  ["Ben E.", "Thanks for leaving the errors in and showing how you fixed them.", "Positive", .89, 7, 4],
  ["Anika Q.", "Interesting. I might try this with my own channel.", "Neutral", .52, 6, 5],
  ["Lucas Z.", "Really helpful video! Looking forward to the deployment part.", "Positive", .97, 5, 6],
  ["Zoya I.", "Love the practical examples. This made ML feel much less intimidating.", "Positive", .95, 4, 7],
  ["Theo U.", "Well, that was surprisingly useful 🙂", "Positive", .57, 3, 7],
];

export const demo = {
  video: { title: "Building my first machine learning project", channel: "Learning with Sam",
           duration: "12:48", fetched: 28, skipped: 4 },
  model: { name: "TF-IDF + logistic regression", testAccuracy: .7159368276846787,
           testMacroF1: .7168507688066018, testRows: 103653, version: "V1 baseline" },
  comments: rows.map(([author, text, sentiment, confidence, likes, day], index) => ({
    id: `demo-${index + 1}`, author, text, sentiment, confidence, likes,
    publishedAt: `2026-10-0${day}T12:00:00Z`,
  })),
};
