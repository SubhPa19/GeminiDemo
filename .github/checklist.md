# Android Pull Request Checklist

Please review and confirm the following before requesting review.

## Code Quality

* [ ] Code follows project coding standards
* [ ] No unused imports, variables, or dead code
* [ ] Meaningful variable and method names used
* [ ] Code is modular and readable

## Architecture

* [ ] Changes follow project architecture (MVVM / MVI / Clean Architecture)
* [ ] No business logic inside Activities or Fragments
* [ ] ViewModels are lifecycle-safe
* [ ] Dependency injection used properly (Hilt/Dagger/Koin)

## Performance

* [ ] No heavy operations on Main Thread
* [ ] Long running tasks moved to background thread
* [ ] Efficient RecyclerView usage
* [ ] Avoid unnecessary recompositions (Compose)

## Memory & Lifecycle

* [ ] No Context leaks
* [ ] Proper lifecycle handling
* [ ] Observers removed when required
* [ ] Coroutines scoped correctly

## UI / UX

* [ ] UI tested on multiple screen sizes
* [ ] Accessibility considered
* [ ] Dark mode supported
* [ ] Strings externalized to resources

## Testing

* [ ] Manual testing completed

## Build & CI

* [ ] Project builds successfully

## Security

* [ ] No API keys or secrets committed
* [ ] Sensitive data handled securely

## Documentation

* [ ] README or documentation updated if required
* [ ] Complex logic documented with comments

## PR Quality

* [ ] PR title clearly describes change
* [ ] PR description explains purpose and impact
* [ ] Screenshots attached if UI changes

---

### AI Review

* [ ] Gemini AI PR review executed
* [ ] Critical issues resolved
* [ ] AI suggestions reviewed
