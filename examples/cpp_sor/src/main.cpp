#include "mockx_sor/app.hpp"
#include "mockx_sor/config.hpp"

#include <exception>
#include <iostream>

int main(int argc, char** argv) {
    try {
        const auto command = mockx::sor::parse_command_line(argc, argv);
        if (command.help) {
            std::cout << mockx::sor::usage();
            return 0;
        }
        if (command.version) {
            std::cout << "mockx_sor 0.1.0\n";
            return 0;
        }
        return mockx::sor::run_sor(command);
    } catch (const mockx::sor::CommandLineError& error) {
        std::cerr << error.what() << "\n\n" << mockx::sor::usage();
        return 2;
    } catch (const mockx::sor::DecimalError& error) {
        std::cerr << error.what() << "\n\n" << mockx::sor::usage();
        return 2;
    } catch (const mockx::sor::ConfigError& error) {
        std::cerr << error.what() << '\n';
        return 2;
    } catch (const mockx::sor::RoutingError& error) {
        std::cerr << error.what() << '\n';
        return 5;
    } catch (const std::exception& error) {
        std::cerr << "SOR failed: " << error.what() << '\n';
        return 4;
    }
}
