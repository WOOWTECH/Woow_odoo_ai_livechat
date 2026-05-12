async page => {
  // Navigate to the livechat channel in discuss
  await page.goto('http://localhost:9094/odoo/discuss/channel/11');
  await page.waitForTimeout(5000);

  // Find the composer and type a message
  // Odoo 18 uses contenteditable div for the composer
  const composer = page.locator('.o-mail-Composer-input').first();
  await composer.click();
  await composer.fill('這是管理員的測試訊息，不應該觸發AI');
  await composer.press('Enter');

  await page.waitForTimeout(3000);
  return "Admin message sent";
}
