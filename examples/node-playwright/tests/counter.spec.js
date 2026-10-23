import { expect, test } from '@playwright/test';

test('counter increments and resets without reloading', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'A worktree, a stack, a counter' })).toBeVisible();
  const counter = page.getByRole('button', { name: /^Count:/ });
  await expect(counter).toHaveText('Count: 0');
  await counter.click();
  await counter.click();
  await expect(counter).toHaveText('Count: 2');
  await page.getByRole('button', { name: 'Reset', exact: true }).click();
  await expect(counter).toHaveText('Count: 0');
});
