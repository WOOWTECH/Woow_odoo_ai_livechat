async page => {
  // Click chat button
  const chatBtn = page.locator('.o-livechat-root').locator('button').first();
  await chatBtn.click();
  await page.waitForTimeout(2000);

  const messages = [
    "第一個問題：你們週末有營業嗎？",
    "第二個問題：可以電話訂購嗎？",
    "第三個問題：有會員折扣嗎？"
  ];

  // Send all 3 messages rapidly within ~3 seconds
  for (const msg of messages) {
    const input = page.locator('.o-livechat-root').locator('textarea').first();
    await input.fill(msg);
    await input.press('Enter');
    await page.waitForTimeout(800); // ~0.8s between each
  }

  // Now wait for all AI replies (up to 60 seconds total)
  // We expect 6 messages total (3 user + 3 AI)
  let finalCount = 0;
  for (let i = 0; i < 120; i++) {
    await page.waitForTimeout(500);
    finalCount = await page.locator('.o-livechat-root').locator('.o-mail-Message').count();
    if (finalCount >= 6) break;
  }

  // Collect all messages
  const allMsgs = [];
  const msgElements = page.locator('.o-livechat-root').locator('.o-mail-Message');
  const count = await msgElements.count();
  for (let i = 0; i < count; i++) {
    const text = await msgElements.nth(i).textContent();
    allMsgs.push(text.substring(0, 150));
  }

  return JSON.stringify({
    totalMessages: finalCount,
    expectedMessages: 6,
    allPassed: finalCount >= 6,
    messages: allMsgs
  }, null, 2);
}
