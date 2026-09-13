from .cli import main

if __name__ == "__main__":
    # The guard matters: multiprocessing's spawn start method re-imports this
    # module in every worker, and without it each worker would launch a server.
    raise SystemExit(main())
