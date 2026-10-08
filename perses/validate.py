"""Compatibility alias for the current project-aware entry point."""
if __name__ == "__main__":
    import sys
    from project_release import main
    sys.argv.insert(1, 'audit-published')
    main()
