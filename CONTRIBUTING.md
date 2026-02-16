# Contributing to Chat2API

Thank you for your interest in contributing to Chat2API! 🎉

## How to Contribute

### Reporting Bugs

1. Check if the bug has already been reported in [Issues](https://github.com/YOUR_USERNAME/Chat2API/issues)
2. If not, create a new issue with:
   - Clear title and description
   - Steps to reproduce
   - Expected vs actual behavior
   - Environment details (OS, Python version, etc.)
   - Relevant logs or screenshots

### Suggesting Features

1. Check existing [Issues](https://github.com/YOUR_USERNAME/Chat2API/issues) and [Discussions](https://github.com/YOUR_USERNAME/Chat2API/discussions)
2. Create a new issue with:
   - Clear description of the feature
   - Use cases and benefits
   - Possible implementation approach

### Pull Requests

1. Fork the repository
2. Create a new branch: `git checkout -b feature/your-feature-name`
3. Make your changes
4. Test thoroughly
5. Commit with clear messages
6. Push to your fork
7. Create a Pull Request

## Development Setup

```bash
# Clone your fork
git clone https://github.com/YOUR_USERNAME/Chat2API.git
cd Chat2API

# Install dependencies
pip install -r requirements.txt

# Setup cookies (see README.md)
cd poc
python3 test_save_cookies.py

# Run tests
cd ..
python3 test_api.py
```

## Code Style

- Follow PEP 8
- Use type hints where appropriate
- Add docstrings to functions and classes
- Keep functions focused and small

## Testing

- Add tests for new features
- Ensure all existing tests pass
- Test both streaming and non-streaming modes
- Test error cases

## Commit Messages

Use clear, descriptive commit messages:

```
feat: add support for Claude provider
fix: handle cookie expiration gracefully
docs: update README with deployment guide
refactor: simplify response parsing logic
```

## Security

- Never commit `cookies.json` or `.env` files
- Review [SECURITY.md](SECURITY.md) before contributing
- Report security issues privately

## Adding New Providers

To add a new AI provider:

1. Create `chat2api/providers/your_provider.py`
2. Implement the client class
3. Add to `chat2api/server.py`
4. Update documentation
5. Add tests

## Documentation

- Update README.md for user-facing changes
- Add docstrings for new code
- Update examples if API changes

## Questions?

Feel free to:
- Open a [Discussion](https://github.com/YOUR_USERNAME/Chat2API/discussions)
- Ask in existing Issues
- Check the [README](README.md) and [Documentation](docs/)

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
