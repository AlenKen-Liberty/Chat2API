# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

If you discover a security vulnerability in Chat2API, please report it by:

1. **Do NOT** open a public issue
2. Email the maintainer directly (or create a private security advisory on GitHub)
3. Include:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

## Security Best Practices

### Cookie Management

**IMPORTANT**: Never commit your `cookies.json` file to version control!

- Cookies contain your Perplexity session credentials
- The `.gitignore` file is configured to exclude `cookies.json`
- Always verify before committing: `git status`

### Environment Variables

- Never commit `.env` files with real credentials
- Use `.env.example` as a template
- Store sensitive data in environment variables, not in code

### API Keys

- This project does NOT use API keys
- The "dummy" API key in examples is required by OpenAI client library but not actually used
- No real API keys are needed or stored

### Deployment

When deploying to production:

1. Use HTTPS (reverse proxy with nginx/caddy)
2. Implement rate limiting
3. Add authentication if exposing publicly
4. Rotate cookies periodically
5. Monitor for unusual activity

## Known Security Considerations

### Cookie Expiration

- Cookies expire based on Perplexity's session timeout
- Currently set to expire in 2027 (long-lived)
- Implement auto-refresh mechanism for production use

### No Built-in Authentication

- Current version has no authentication
- Suitable for local/private use only
- Add authentication layer before public deployment

### Rate Limiting

- No built-in rate limiting
- Implement rate limiting for production
- Respect Perplexity's usage limits

## Security Checklist for Contributors

Before submitting a PR:

- [ ] No hardcoded credentials
- [ ] No committed `cookies.json` files
- [ ] No committed `.env` files with real values
- [ ] Sensitive data properly gitignored
- [ ] Dependencies up to date
- [ ] No known vulnerabilities in dependencies

## Dependency Security

Run security audit regularly:

```bash
pip install safety
safety check -r requirements.txt
```

## Contact

For security concerns, please contact the project maintainer.
