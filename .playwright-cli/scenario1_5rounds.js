async page => {
  const questions = [
    "你好，請問你們有賣筆記型電腦嗎？",
    "那請問最便宜的筆電價格是多少？",
    "好的，請問你們有提供免費配送服務嗎？",
    "如果商品有問題，退貨流程是怎樣的？",
    "最後一個問題，我可以用信用卡分期付款嗎？"
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
        results.push({ round: i+1, question: questions[i], reply: text.substring(0, 200), msgCount: currentCount });
        found = true;
        break;
      }
    }
    if (!found) {
      results.push({ round: i+1, question: questions[i], reply: "TIMEOUT", msgCount: -1 });
    }
    await page.waitForTimeout(1500);
  }

  return JSON.stringify(results, null, 2);
}
