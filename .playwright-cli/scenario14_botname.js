async page => {
  await page.waitForTimeout(3000);
  const chatBtn = page.locator('.o-livechat-root').locator('button').first();
  await chatBtn.click();
  await page.waitForTimeout(2000);

  const input = page.locator('.o-livechat-root').locator('textarea').first();
  await input.fill("你好，請問你是誰？你叫什麼名字？");
  await input.press('Enter');

  const beforeCount = await page.locator('.o-livechat-root').locator('.o-mail-Message').count();
  for (let i = 0; i < 40; i++) {
    await page.waitForTimeout(500);
    const currentCount = await page.locator('.o-livechat-root').locator('.o-mail-Message').count();
    if (currentCount > beforeCount) {
      // Get the last message
      const lastMsg = page.locator('.o-livechat-root').locator('.o-mail-Message').last();
      const text = await lastMsg.textContent();

      // Try to find the author/name display in the message area
      const allMsgs = await page.locator('.o-livechat-root').locator('.o-mail-Message').all();
      const msgDetails = [];
      for (const msg of allMsgs) {
        const content = await msg.textContent();
        msgDetails.push(content.substring(0, 150));
      }

      return JSON.stringify({
        reply: text.substring(0, 300),
        allMessages: msgDetails,
        msgCount: currentCount
      });
    }
  }
  return JSON.stringify({ reply: "TIMEOUT" });
}
