async page => {
  const questions = [
    "你好，我想問一下產品保固期是多久？",
    "Thank you. What is your return policy for electronics?",
    "すみません、配送にかかる日数を教えてください。"
  ];

  const results = [];

  for (let i = 0; i < questions.length; i++) {
    const input = page.locator('.o-livechat-root').locator('textarea').first();
    await input.fill(questions[i]);
    await input.press('Enter');

    const beforeCount = await page.locator('.o-livechat-root').locator('.o-mail-Message').count();

    let found = false;
    for (let j = 0; j < 60; j++) {
      await page.waitForTimeout(500);
      const currentCount = await page.locator('.o-livechat-root').locator('.o-mail-Message').count();
      if (currentCount > beforeCount) {
        const lastMsg = page.locator('.o-livechat-root').locator('.o-mail-Message').last();
        const text = await lastMsg.textContent();
        results.push({
          round: i+1,
          lang: ["中文", "English", "日本語"][i],
          question: questions[i],
          reply: text.substring(0, 250)
        });
        found = true;
        break;
      }
    }
    if (!found) {
      results.push({ round: i+1, lang: ["中文", "English", "日本語"][i], question: questions[i], reply: "TIMEOUT" });
    }
    // Wait longer between rounds to avoid serialization
    await page.waitForTimeout(5000);
  }

  return JSON.stringify(results, null, 2);
}
