import sys, time

while True:
    try:
        import env_display   # runs forever inside its own while True loop
    except Exception as e:
        sys.print_exception(e)
        time.sleep(5)        # brief pause before restart so serial can be read
    # If env_display exits or crashes, reset and try again
    import machine
    machine.reset()
