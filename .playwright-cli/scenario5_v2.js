async page => {
  const chatBtn = page.locator('.o-livechat-root').locator('button').first();
  await chatBtn.click();
  await page.waitForTimeout(2000);

  const input = page.locator('.o-livechat-root').locator('textarea').first();
  await input.fill("你好，我是訪客B，請問你們的客服電話是幾號？");
  await input.press('Enter');

  const beforeCount = await page.locator('.o-livechat-root').locator('.o-mail-Message').count();
  for (let i = 0; i < 60; i++) {
    await page.waitForTimeout(500);
    const currentCount = await page.locator('.o-livechat-root').locator('.o-mail-Message').count();
    if (currentCount > beforeCount) {
      const lastMsg = page.locator('.o-livechat-root').locator('.o-mail-Message').last();
      const text = await lastMsg.textContent();
      return JSON.stringify({ visitor: "B", success: true, reply: text.substring(0, 250), msgCount: currentCount });
    }
  }
  return JSON.stringify({ visitor: "B", success: false, reply: "TIMEOUT" });
}
