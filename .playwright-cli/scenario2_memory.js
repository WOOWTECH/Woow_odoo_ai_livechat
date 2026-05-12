async page => {
  // Click chat button
  const chatBtn = page.locator('.o-livechat-root').locator('button').first();
  await chatBtn.click();
  await page.waitForTimeout(2000);

  const questions = [
    "你好，我叫小明，請記住我的名字",
    "我剛才說我叫什麼名字？"
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
        results.push({ round: i+1, question: questions[i], reply: text.substring(0, 300) });
        found = true;
        break;
      }
    }
    if (!found) {
      results.push({ round: i+1, question: questions[i], reply: "TIMEOUT" });
    }
    await page.waitForTimeout(2000);
  }

  return JSON.stringify(results, null, 2);
}
