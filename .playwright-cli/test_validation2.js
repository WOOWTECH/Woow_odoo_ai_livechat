async (page) => {
  const results = [];

  // Helper to capture errors, save, and handle the error dialog
  async function testField(testName, setupFn) {
    // Navigate fresh each time
    await page.goto('http://localhost:9094/odoo/livechat/1');
    await page.waitForTimeout(2000);

    // Click AI Integration tab
    await page.evaluate(() => {
      var tab = document.querySelector('a[name="ai_integration"]');
      if (tab) tab.click();
    });
    await page.waitForTimeout(500);

    // Set up error capture
    await page.evaluate(() => {
      window._testErrors = [];
      const obs = new MutationObserver(function(muts) {
        muts.forEach(function(m) {
          m.addedNodes.forEach(function(n) {
            if (n.nodeType !== 1 || !n.querySelector) return;
            var content = n.querySelector('.o_notification_content, .modal-body');
            if (content) window._testErrors.push(content.textContent.trim());
          });
        });
      });
      obs.observe(document.body, {childList: true, subtree: true});
    });

    // Apply the field change
    await setupFn(page);

    // Try to save
    await page.evaluate(() => {
      var btn = document.querySelector('.o_form_button_save');
      if (btn) btn.click();
    });
    await page.waitForTimeout(3000);

    // Collect errors
    var errors = await page.evaluate(() => window._testErrors);
    results.push({test: testName, errors: errors, passed: errors.length > 0});
  }

  // Test 1: Invalid URL (ftp protocol)
  await testField('Invalid URL (ftp)', async (p) => {
    await p.locator('[name="ai_api_base_url"] input').fill('ftp://invalid.com');
  });

  // Test 2: Temperature > 2.0
  await testField('Temperature > 2.0', async (p) => {
    await p.locator('[name="ai_temperature"] input').fill('2.5');
  });

  // Test 3: Temperature < 0
  await testField('Temperature < 0', async (p) => {
    await p.locator('[name="ai_temperature"] input').fill('-0.5');
  });

  // Test 4: Max History = 0
  await testField('Max History = 0', async (p) => {
    await p.locator('[name="ai_max_history"] input').fill('0');
  });

  // Test 5: Max History > 200
  await testField('Max History > 200', async (p) => {
    await p.locator('[name="ai_max_history"] input').fill('201');
  });

  // Test 6: Empty API Key
  await testField('Empty API Key (required)', async (p) => {
    await p.locator('[name="ai_api_key"] input').fill('');
  });

  return results;
}
